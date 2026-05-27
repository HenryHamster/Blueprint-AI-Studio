import { vi, describe, it, expect, beforeEach, afterEach, beforeAll } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
  // Keep module-level provider/auth defaults deterministic across local and CI
  // environments. Some test cases call helpers without explicit provider args.
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  // Clear before index.ts loads so the module-level LLM_CLI_TRANSLATION
  // constant is null. Tests that verify translation behavior set the env var
  // themselves and call parseLLMCliTranslation() directly.
  delete process.env.LLM_CLI_TRANSLATION;
  return { mockReadFileSync };
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('fs', () => ({
  default: {
    readFileSync: mockReadFileSync,
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    openSync: vi.fn().mockReturnValue(3),
    closeSync: vi.fn(),
    chmodSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  spawn: vi.fn().mockReturnValue({
    stdin: { on: vi.fn(), end: vi.fn() },
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execSync: vi.fn().mockReturnValue(''),
  // reset.ts (now imported transitively via index.ts) wraps execFile with
  // promisify; supply the (cmd, args, opts, callback) shape so reset's
  // own dependency surface doesn't break unrelated index tests.
  execFile: (
    _cmd: string,
    _args: string[],
    _opts: unknown,
    cb: (err: Error | null, stdout: string, stderr: string) => void,
  ) => {
    void Promise.resolve().then(() => { cb(null, '', ''); });
  },
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Imports (after mocks are registered) ─────────────────────────────────────
import { strVal, sanitize, validateBranchName, readHandlerOutput, testFixLoop, errorFingerprint, captureTestBaseline, RateLimitError, setupWorkspace, pushAndCreatePR, condensedAnalysisSections, matchesRateLimit, parseFailingTests, parseFailingTestsFromVitestJson, extractPytestSelectorsFromOutput, normalizePytestSelectorForRunner, buildTargetedPytestRunners, recordToolUse, snapshotToolTrace, extractInvestigatedFiles, clearToolTrace, appendToLogBuffer, flushLogBuffer, snapshotLogBuffer, getProviderRunner, parseLLMCliTranslation, execFileAsync, runProviderAgent, persistAndPostStatus, parseStaleResultFile, markStaleResultFailure, shouldAbandonStaleResult, pollForNextRole, _resetTelemetryForTesting, _setRoleClaimTimestampForTesting, _getFirstToolUseAtForTesting, _setWarmStartForTesting, _setHandlerDurationForTesting, _setAgentRuntimeFingerprintForTesting, _startHeartbeatForTesting, _stopHeartbeatForTesting, getAgentHeaders, ClaudeTimeoutError, jaccardSimilarity, detectLoopDivergence, fetchVmImageIdentity, recordLlmCallStart, recordLlmTokens, getLlmTelemetry, resetLlmTelemetry, estimateTokensFromChars, deriveTaskBranch, TASK_TYPE_BRANCH_PREFIX, codexStreamHasTurnCompleted, type FailureCategory } from './index.js';
import { clearPidRegistry, _getRegisteredPids } from './processTracking.js';
import {
  drainFixLoopEventBuffer,
  _resetFixLoopEventStateForTesting,
} from './fix-loop-events.js';
import {
  MAX_TEST_FIX_ATTEMPTS,
  TOOL_USE_STALL_TIMEOUT_MS,
  CLAUDE_STREAM_STALL_MS,
} from './constants.js';
import { execFileSync, spawn, execSync } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';

/** Asserts a value is defined and returns it (narrowed). Replaces unsafe `x!` in tests. */
function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}

// ── strVal ───────────────────────────────────────────────────────────────────
describe('strVal', () => {
  it('returns string values unchanged', () => {
    expect(strVal('hello')).toBe('hello');
  });

  it('returns empty string for undefined', () => {
    expect(strVal(undefined)).toBe('');
  });

  it('returns fallback for null', () => {
    expect(strVal(null, 'default')).toBe('default');
  });

  it('returns fallback (empty) for numbers', () => {
    expect(strVal(42)).toBe('');
  });

  it('returns custom fallback for numbers', () => {
    expect(strVal(42, 'n/a')).toBe('n/a');
  });

  it('returns empty string for empty string input', () => {
    expect(strVal('')).toBe('');
  });

  it('uses custom fallback when value is not a string', () => {
    expect(strVal(false, 'fb')).toBe('fb');
  });

  it('returns string for truthy non-string value with fallback', () => {
    expect(strVal({}, 'obj')).toBe('obj');
  });
});

// ── sanitize ─────────────────────────────────────────────────────────────────
describe('sanitize', () => {
  it('preserves text that contains no secrets', () => {
    const text = 'Hello, world! No secrets here.';
    expect(sanitize(text)).toBe(text);
  });

  it('redacts ghp_ GitHub tokens', () => {
    expect(sanitize('token: ghp_AbCdEfGhIjKlMnOp123')).toBe('token: ghp_***');
  });

  it('redacts ghs_ GitHub app tokens', () => {
    expect(sanitize('ghs_ABCdef123XYZ')).toBe('ghs_***');
  });

  it('redacts github_pat_ personal access tokens', () => {
    expect(sanitize('github_pat_11ABCDE_fooBar_baz_QUX')).toBe('github_pat_***');
  });

  it('redacts sk-ant- Anthropic API keys', () => {
    expect(sanitize('key: sk-ant-api03-someverylongkey-abc')).toBe('key: sk-ant-***');
  });

  it('redacts JWT tokens matching eyJ…eyJ…sig pattern', () => {
    const jwt =
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c';
    expect(sanitize(jwt)).toBe('eyJ***');
  });

  it('redacts Bearer tokens in Authorization headers', () => {
    expect(sanitize('Authorization: Bearer abc123secrettoken')).toBe(
      'Authorization: Bearer ***',
    );
  });

  it('redacts oauth_token= query parameter values', () => {
    const result = sanitize('url?oauth_token=supersecret&other=ok');
    expect(result).toContain('oauth_token=***');
    expect(result).not.toContain('supersecret');
  });

  it('redacts PEM RSA private keys', () => {
    const pem =
      '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----';
    expect(sanitize(pem)).toBe('***PRIVATE KEY***');
  });

  it('redacts PEM EC private keys', () => {
    const pem =
      '-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEIO\n-----END EC PRIVATE KEY-----';
    expect(sanitize(pem)).toBe('***PRIVATE KEY***');
  });

  it('redacts x-access-token URLs', () => {
    expect(
      sanitize('https://x-access-token:ghp_abc123@github.com/org/repo.git'),
    ).toBe('https://x-access-token:***@github.com/org/repo.git');
  });

  it('redacts multiple secret types in a single string', () => {
    const text =
      'token: ghp_ABC123 and key: sk-ant-api03-XYZ and Authorization: Bearer mytoken123';
    const result = sanitize(text);
    expect(result).not.toContain('ghp_ABC123');
    expect(result).not.toContain('sk-ant-api03-XYZ');
    expect(result).not.toContain('mytoken123');
    expect(result).toContain('ghp_***');
    expect(result).toContain('sk-ant-***');
    expect(result).toContain('Bearer ***');
  });
});

// ── validateBranchName ───────────────────────────────────────────────────────
describe('validateBranchName', () => {
  it('accepts and returns a simple branch name', () => {
    expect(validateBranchName('main', 'branch')).toBe('main');
  });

  it('accepts feature branch with slash', () => {
    expect(validateBranchName('feature/foo', 'branch')).toBe('feature/foo');
  });

  it('accepts branch name with underscore', () => {
    expect(validateBranchName('fix_bar', 'branch')).toBe('fix_bar');
  });

  it('accepts version-like branch name with dots', () => {
    expect(validateBranchName('v1.2.3', 'branch')).toBe('v1.2.3');
  });

  it('accepts nested path branch', () => {
    expect(validateBranchName('blueprint/fix-123', 'branch')).toBe('blueprint/fix-123');
  });

  it('throws on empty string with label in message', () => {
    expect(() => validateBranchName('', 'base branch')).toThrow('base branch is required');
  });

  it('throws on directory traversal (..)', () => {
    expect(() => validateBranchName('feature/../hack', 'branch')).toThrow(
      'Invalid branch (contains ..)',
    );
  });

  it('throws on leading hyphen (git flag injection risk)', () => {
    expect(() => validateBranchName('-evil-flag', 'branch')).toThrow(
      'Invalid branch (starts with -)',
    );
  });

  it('throws on space in branch name', () => {
    expect(() => validateBranchName('feature branch', 'branch')).toThrow('Invalid branch');
  });

  it('throws on semicolon (shell injection risk)', () => {
    expect(() => validateBranchName('feat;rm -rf /', 'branch')).toThrow('Invalid branch');
  });

  it('throws on @ character', () => {
    expect(() => validateBranchName('feat@evil', 'branch')).toThrow('Invalid branch');
  });

  it('throws on backtick character', () => {
    expect(() => validateBranchName('feat`cmd`', 'branch')).toThrow('Invalid branch');
  });
});

// ── deriveTaskBranch ─────────────────────────────────────────────────────────
// Pins the task-type → branch-prefix mapping so drift between the agent and
// backend/app/services.py _TASK_TYPE_BRANCH_PREFIX is caught immediately.
describe('deriveTaskBranch', () => {
  const taskId = 'abcdef12-3456-7890-abcd-ef1234567890';
  const shortId = 'abcdef12'; // taskId.replace(/-/g,'').slice(0,8)

  it('maps refactor → refactor prefix', () => {
    expect(deriveTaskBranch('refactor', taskId)).toBe(`blueprint/refactor-${shortId}`);
  });

  it('maps docs → docs prefix', () => {
    expect(deriveTaskBranch('docs', taskId)).toBe(`blueprint/docs-${shortId}`);
  });

  it('maps deps → deps prefix', () => {
    expect(deriveTaskBranch('deps', taskId)).toBe(`blueprint/deps-${shortId}`);
  });

  it('maps migrate → migrate prefix', () => {
    expect(deriveTaskBranch('migrate', taskId)).toBe(`blueprint/migrate-${shortId}`);
  });

  it('defaults unknown types to task prefix', () => {
    expect(deriveTaskBranch('create', taskId)).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('qa', taskId)).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('fix', taskId)).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('review', taskId)).toBe(`blueprint/task-${shortId}`);
    expect(deriveTaskBranch('', taskId)).toBe(`blueprint/task-${shortId}`);
  });

  it('TASK_TYPE_BRANCH_PREFIX covers all explicitly-mapped types', () => {
    // Keep in sync with backend/app/shared_state.py TASK_TYPE_BRANCH_PREFIX
    expect(TASK_TYPE_BRANCH_PREFIX).toEqual({
      refactor: 'refactor',
      docs: 'docs',
      deps: 'deps',
      migrate: 'migrate',
    });
  });

  it('uses first 8 hex chars of task UUID with dashes removed', () => {
    const id = '11223344-aaaa-bbbb-cccc-ddddeeee0000';
    expect(deriveTaskBranch('refactor', id)).toBe('blueprint/refactor-11223344');
  });

  it('accepts a per-project namespace argument', () => {
    const id = '11223344-aaaa-bbbb-cccc-ddddeeee0000';
    expect(deriveTaskBranch('refactor', id, 'autoproj')).toBe('autoproj/refactor-11223344');
    expect(deriveTaskBranch('docs', id, 'bot')).toBe('bot/docs-11223344');
    // Default still matches the Blueprint self-repo namespace byte-for-byte.
    expect(deriveTaskBranch('refactor', id)).toBe('blueprint/refactor-11223344');
  });
});

// ── FailureCategory ──────────────────────────────────────────────────────────
describe('FailureCategory', () => {
  const expectedCategories: FailureCategory[] = [
    'clone_failed',
    'build_failed',
    'test_failed',
    'lint_failed',
    'pr_creation_failed',
    'claude_code_failed',
    'parse_failed',
    'fatal',
  ];

  it('covers all expected failure category values', () => {
    // This test ensures the FailureCategory type includes all expected values.
    // If a new category is added to the type but not here, TypeScript will catch it.
    for (const cat of expectedCategories) {
      const value: FailureCategory = cat;
      expect(typeof value).toBe('string');
    }
    expect(expectedCategories).toHaveLength(8);
  });
});

// ── readHandlerOutput ─────────────────────────────────────────────────────────
describe('readHandlerOutput', () => {
  beforeEach(() => {
    mockReadFileSync.mockReset();
  });

  it('returns parsed object when file contains valid JSON', () => {
    mockReadFileSync.mockReturnValue('{"result": "ok", "count": 3}');
    const result = readHandlerOutput('task-1', 'research_findings', null);
    expect(result).toEqual({ result: 'ok', count: 3 });
  });

  it('returns parsed array when file contains valid JSON array', () => {
    mockReadFileSync.mockReturnValue('[{"title": "Finding 1"}, {"title": "Finding 2"}]');
    const result = readHandlerOutput('task-2', 'plan_output', null);
    expect(result).toEqual([{ title: 'Finding 1' }, { title: 'Finding 2' }]);
  });

  it('extracts embedded JSON array via regex when top-level parse fails', () => {
    const raw =
      'Some prefix text here\n[{"id": 1, "name": "item"}]\nSome trailing text';
    mockReadFileSync.mockReturnValue(raw);
    const result = readHandlerOutput(
      'task-3',
      'output',
      null,
    ) as Array<{ id: number; name: string }> | null;
    expect(result).toEqual([{ id: 1, name: 'item' }]);
  });

  it('extracts embedded strategic output object via regex fallback', () => {
    // Use an empty direction_operations array so the array-pattern regex
    // (/\[\s*\{...\}/) does not match first — the strategic-object regex is
    // only reached when there is no embedded JSON array in the raw text.
    const strategicObj =
      '{"vision": "build great things", "direction_operations": []}';
    const raw = `Preamble text with no JSON arrays\n${strategicObj}`;
    mockReadFileSync.mockReturnValue(raw);
    const result = readHandlerOutput(
      'task-4',
      'strategic_output',
      null,
    ) as { vision: string; direction_operations: unknown[] } | null;
    expect(result).not.toBeNull();
    if (!result) throw new Error('expected result');
    expect(result.vision).toBe('build great things');
    expect(Array.isArray(result.direction_operations)).toBe(true);
  });

  it('returns null when file is not found (ENOENT)', () => {
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT: no such file or directory'), { code: 'ENOENT' });
    });
    const result = readHandlerOutput('task-5', 'missing', null);
    expect(result).toBeNull();
  });

  it('returns null for completely unparseable content with no recognisable JSON', () => {
    mockReadFileSync.mockReturnValue(
      'This is plain text with no JSON arrays or strategic objects at all.',
    );
    const result = readHandlerOutput('task-6', 'bad_output', null);
    expect(result).toBeNull();
  });

  it('reads from the expected /tmp path derived from taskId and outputType', () => {
    mockReadFileSync.mockReturnValue('{"ok": true}');
    readHandlerOutput('myTask', 'review_verdict', null);
    expect(mockReadFileSync).toHaveBeenCalledWith('/tmp/review_verdict_myTask.json', 'utf-8');
  });
});
// Helper: spawn mock that emits close(code) immediately for each spawn call.
// `failCmds` maps command names to (exitCode, stderr) for failing runners.
// `npm ci` (dep install) is never failed even when `npm` is in failCmds —
// testFixLoop now treats install failures as setup errors (not test
// failures), so tests that want to simulate a failing test run must let
// the install step succeed.
function configureSpawnMock(failCmds: Record<string, { exitCode: number; stderr: string }> = {}) {
  vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
    const cmdStr = String(cmd);
    const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
    const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
    const failure = isNpmInstall ? undefined : failCmds[cmdStr];
    const stderrCbs: ((d: Buffer) => void)[] = [];
    const proc = {
      pid: 100 + Math.floor(Math.random() * 1000),
      stderr: {
        on: vi.fn((event: string, cb: (d: Buffer) => void) => {
          if (event === 'data') stderrCbs.push(cb);
        }),
      },
      stdout: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') {
          setImmediate(() => {
            if (failure) for (const scb of stderrCbs) scb(Buffer.from(failure.stderr));
            cb(failure ? failure.exitCode : 0);
          });
        }
      }),
      kill: vi.fn(),
    };
    return proc as unknown as ReturnType<typeof spawn>;
  });
}

// ── testFixLoop failureSummary ────────────────────────────────────────────────
describe('testFixLoop failureSummary', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    // git() helper at index.ts:507 calls .trim() on the return — give it a
    // string default so unmocked git calls (rev-parse, add -A, etc.) don't crash.
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
  });

  it('returns undefined failureSummary when tests pass', async () => {
    // Provide a buildConfig with a test_cmd so a runner is created
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);

    expect(result.passed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.failureSummary).toBeUndefined();
  });

  it('returns setupFailed=true (not passed) when npm ci fails for every runner', async () => {
    // Regression: previously npm ci failures were swallowed as "runner
    // skipped" and the whole call returned `{passed: true}`, which let
    // the handler push code that was never test-verified. setupFailed
    // propagates through so standard/review-fix handlers mark the task
    // FAILED with failure_category='build_failed' instead of merging.
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    // Fail ALL npm calls — including ci — to simulate dep-install breakage.
    vi.mocked(spawn).mockImplementation((cmd: unknown) => {
      const cmdStr = String(cmd);
      const stderrCbs: ((d: Buffer) => void)[] = [];
      const fail = cmdStr === 'npm';
      const proc = {
        pid: 100,
        stderr: { on: vi.fn((event: string, cb: (d: Buffer) => void) => { if (event === 'data') stderrCbs.push(cb); }) },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (fail) for (const scb of stderrCbs) scb(Buffer.from('npm ERR! registry unreachable'));
              cb(fail ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.setupFailed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.failureSummary).toMatch(/npm ci failed/);
    // Claude MUST NOT be invoked for setup failures — check that no
    // `claude` spawn happened even though a runner was configured.
    const claudeCalls = vi.mocked(spawn).mock.calls.filter(c => c[0].includes('claude')).length;
    expect(claudeCalls).toBe(0);
  });

  it('returns failureSummary with combined failure messages when tests fail after all attempts', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    const errorMessage = 'Error: Test suite failed\n  at Object.<anonymous> (test.ts:10:5)';
    // npm (the test runner) exits non-zero with error on stderr; claude (spawn)
    // and other commands still succeed so runClaudeCode resolves for the fix loop.
    configureSpawnMock({ npm: { exitCode: 1, stderr: errorMessage } });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.failureSummary).toBeDefined();
    expect(result.failureSummary).toContain(errorMessage);
  });
});

// ── testFixLoop directory scoping ─────────────────────────────────────────────
describe('testFixLoop directory scoping', () => {
  const multiRunnerConfig = {
    directories: [
      { path: 'agent', type: 'node', test_cmd: 'npm test' },
      { path: 'backend', type: 'python', test_cmd: 'python3 -m pytest' },
    ],
  };

  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
  });

  function cwdsCalledWith(): (string | undefined)[] {
    // testFixLoop's runners go through execFileAsync which calls spawn with cwd
    // in options[2].  Claude Code runs via spawn too but always cwd=WORKSPACE,
    // so filtering on the command (npm/python3) excludes those.
    return vi.mocked(spawn).mock.calls
      .filter(c => c[0] === 'npm' || c[0] === 'python3')
      .map(c => (c[2] as { cwd?: string } | undefined)?.cwd);
  }

  it('runs all runners when relatedFiles is not provided', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('filters to agent runner when relatedFiles contains only agent/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['agent/src/index.ts']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(false);
  });

  it('filters to backend runner when relatedFiles contains only backend/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['backend/app/main.py']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(false);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('runs all runners when relatedFiles contains only non-agent/backend files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['admin-ui/src/App.tsx']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });

  it('runs both runners when relatedFiles contains both agent/ and backend/ files', async () => {
    await testFixLoop(null, { value: '' }, undefined, undefined, multiRunnerConfig, undefined, ['agent/src/index.ts', 'backend/app/main.py']);
    const cwds = cwdsCalledWith();
    expect(cwds.some(c => c?.includes('/agent'))).toBe(true);
    expect(cwds.some(c => c?.includes('/backend'))).toBe(true);
  });
});

// ── errorFingerprint / duplicate-error early exit ───────────────────────────
describe('errorFingerprint', () => {
  it('normalizes whitespace and truncates to 2000 chars', () => {
    expect(errorFingerprint('  foo\n\tbar  ')).toBe('foo bar');
    expect(errorFingerprint('x'.repeat(3000)).length).toBe(2000);
  });

  it('returns same fingerprint for trivially different whitespace', () => {
    expect(errorFingerprint('err:\n  at foo')).toBe(errorFingerprint('err:  at   foo'));
  });
});

// ── jaccardSimilarity unit tests ─────────────────────────────────────────────
describe('jaccardSimilarity', () => {
  it('returns 1 for identical strings', () => {
    expect(jaccardSimilarity('foo bar baz', 'foo bar baz')).toBe(1);
  });

  it('returns 0 for completely disjoint strings', () => {
    expect(jaccardSimilarity('alpha beta', 'gamma delta')).toBe(0);
  });

  it('returns 1 for two empty strings', () => {
    expect(jaccardSimilarity('', '')).toBe(1);
  });

  it('returns a value ≥ 0.7 for near-identical errors differing only in a line number', () => {
    const a = 'TypeError foo is undefined at line 42 in bar ts';
    const b = 'TypeError foo is undefined at line 47 in bar ts';
    expect(jaccardSimilarity(a, b)).toBeGreaterThanOrEqual(0.7);
  });

  it('returns a value < 0.7 for clearly different errors', () => {
    const a = 'TypeError foo is undefined at line 42';
    const b = 'SyntaxError unexpected token semicolon in completely different module';
    expect(jaccardSimilarity(a, b)).toBeLessThan(0.7);
  });
});

// ── detectLoopDivergence unit tests ──────────────────────────────────────────
describe('detectLoopDivergence', () => {
  it('returns null when history has fewer than 3 entries', () => {
    expect(detectLoopDivergence(['fp1', 'fp2'], ['e1', 'e2'], null, null)).toBeNull();
  });

  it('detects oscillation: A -> B -> A fingerprint pattern', () => {
    const fps = ['fpA', 'fpB', 'fpA'];
    const errs = ['error A text', 'error B text', 'error A text'];
    expect(detectLoopDivergence(fps, errs, null, null)).toBe('divergence_oscillation');
  });

  it('detects Jaccard-similar errors across two consecutive pairs', () => {
    // Each error differs by only 1 token out of 10 — Jaccard ≈ 9/11 ≈ 0.82 > threshold
    const base = 'TypeError property foo undefined at baz module context error';
    const errs = [`${base} one`, `${base} two`, `${base} thr`];
    const fps = errs.map((_e, i) => `fp${i}`);  // all distinct fingerprints
    expect(detectLoopDivergence(fps, errs, null, null)).toBe('divergence_similar_error');
  });

  it('detects diff revert when ≥ 80% of previous iteration lines vanish', () => {
    const prevDiff = { added: new Set(['line_a', 'line_b', 'line_c', 'line_d', 'line_e']), removed: new Set<string>() };
    const currDiff = { added: new Set(['line_x']), removed: new Set<string>() };  // all prev lines gone
    const fps = ['fp1', 'fp2', 'fp3'];
    const errs = ['error 1', 'error 2', 'error 3'];
    expect(detectLoopDivergence(fps, errs, prevDiff, currDiff)).toBe('divergence_diff_revert');
  });

  it('does not flag diff revert when < 80% of previous lines vanish', () => {
    const prevDiff = { added: new Set(['line_a', 'line_b', 'line_c', 'line_d', 'line_e']), removed: new Set<string>() };
    const currDiff = { added: new Set(['line_a', 'line_b', 'line_x']), removed: new Set<string>() };  // 3/5 = 60% gone
    const fps = ['fp1', 'fp2', 'fp3'];
    const errs = ['error 1', 'error 2', 'error 3'];
    expect(detectLoopDivergence(fps, errs, prevDiff, currDiff)).toBeNull();
  });
});

// ── testFixLoop divergence detection ─────────────────────────────────────────
describe('testFixLoop divergence detection', () => {
  const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };

  beforeEach(() => {
    _resetFixLoopEventStateForTesting();
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(execSync).mockReset();
    vi.mocked(execSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
  });

  it('exits with divergence_oscillation when test failures oscillate A→B→A', async () => {
    const errorA = 'FAIL test.ts > alpha unique_error_A';
    const errorB = 'FAIL test.ts > beta unique_error_B';
    const errorSequence = [errorA, errorB, errorA];
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      const cmdStr = String(cmd);
      const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
      const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
      const stderrCbs: ((d: Buffer) => void)[] = [];
      const failStderr = (!isNpmInstall && cmdStr === 'npm') ? errorSequence[Math.min(callCount++, errorSequence.length - 1)] : null;
      const proc = {
        pid: 100 + callCount,
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (failStderr !== null) for (const scb of stderrCbs) scb(Buffer.from(failStderr));
              cb(failStderr !== null ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    const events = drainFixLoopEventBuffer();
    expect(events.some(e => e.exit_reason === 'divergence_oscillation')).toBe(true);
  });
});

describe('testFixLoop early exit on identical errors', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(spawn).mockReset();
  });

  it('exits early when consecutive test errors are identical', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    const errorMessage = 'FAIL test.ts > it works';
    configureSpawnMock({ npm: { exitCode: 1, stderr: errorMessage } });

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    expect(result.passed).toBe(false);
    expect(result.failureSummary).toContain(errorMessage);
    // Count ONLY test-run invocations (not the setup `npm ci` install
    // calls testFixLoop does ahead of each runner). Early exit after the
    // duplicate on attempt 2: at most 2 test runs.
    const testRunCalls = vi.mocked(spawn).mock.calls.filter(
      c => c[0] === 'npm' && Array.isArray(c[1]) && c[1][0] === 'test',
    ).length;
    expect(testRunCalls).toBeLessThanOrEqual(2);
  });

  it('continues when test errors differ between attempts', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      const cmdStr = String(cmd);
      const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
      const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
      const stderrCbs: ((d: Buffer) => void)[] = [];
      let failStderr: string | null = null;
      // Only the test-run (npm test) is simulated as failing. npm ci
      // (install) succeeds — testFixLoop now classifies install failures
      // as setup errors, not test failures.
      if (cmdStr === 'npm' && !isNpmInstall) {
        callCount++;
        failStderr = `FAIL variant ${callCount}`;
      }
      const proc = {
        pid: 100 + callCount,
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              if (failStderr !== null) for (const scb of stderrCbs) scb(Buffer.from(failStderr));
              cb(failStderr !== null ? 1 : 0);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);
    // Count only test-run invocations, skipping the setup `npm ci` calls.
    // testFixLoop runs maxFixAttempts + 1 = MAX_TEST_FIX_ATTEMPTS + 1 times when errors differ each iteration.
    const testRunCalls = vi.mocked(spawn).mock.calls.filter(
      c => c[0] === 'npm' && Array.isArray(c[1]) && c[1][0] === 'test',
    ).length;
    expect(testRunCalls).toBe(MAX_TEST_FIX_ATTEMPTS + 1);
  });
});

// ── RateLimitError ───────────────────────────────────────────────────────────
// Pins the class identity so the outer catch in main() (which uses
// `error instanceof RateLimitError` for classification) can't regress into
// the old cast-and-rematch pattern that silently reclassified rate-limits
// as CODE_ERROR when stderrBuffer was empty.
describe('matchesRateLimit', () => {
  it('detects "you\'ve hit your limit"', () => {
    expect(matchesRateLimit("you've hit your limit")).toBe(true);
  });

  it('detects "you\'ve hit your usage limit"', () => {
    expect(matchesRateLimit("you've hit your usage limit")).toBe(true);
  });

  it('detects "claude ai usage limit reached"', () => {
    expect(matchesRateLimit('claude ai usage limit reached')).toBe(true);
  });

  it('detects "you\'re out of extra usage"', () => {
    expect(matchesRateLimit("you're out of extra usage")).toBe(true);
  });

  it('detects "5-hour limit reached"', () => {
    expect(matchesRateLimit('5-hour limit reached')).toBe(true);
  });

  it('detects "api error: rate limit reached"', () => {
    expect(matchesRateLimit('api error: rate limit reached')).toBe(true);
  });

  it('detects "this request would exceed your account\'s rate limit"', () => {
    expect(matchesRateLimit("this request would exceed your account's rate limit")).toBe(true);
  });

  it('matches case-insensitively (uppercase)', () => {
    expect(matchesRateLimit('API ERROR: RATE LIMIT REACHED')).toBe(true);
  });

  it('matches case-insensitively (mixed case)', () => {
    expect(matchesRateLimit('Claude AI Usage Limit Reached')).toBe(true);
  });

  it('matches when pattern is embedded in surrounding text', () => {
    expect(matchesRateLimit('Error from Anthropic: API Error: Rate limit reached. Please retry later.')).toBe(true);
  });

  it('matches when pattern appears after other log lines', () => {
    expect(matchesRateLimit('line 1\nline 2\nyou\'ve hit your usage limit\nline 4')).toBe(true);
  });

  it('returns false for a bare "429"', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });

  it('returns false for the word "rate" alone', () => {
    expect(matchesRateLimit('increased rate of errors')).toBe(false);
  });

  it('returns false for the word "limit" alone', () => {
    expect(matchesRateLimit('limit exceeded on some resource')).toBe(false);
  });

  it('returns false for "overloaded"', () => {
    expect(matchesRateLimit('server is overloaded')).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(matchesRateLimit('')).toBe(false);
  });

  it('returns false for unrelated error output', () => {
    expect(matchesRateLimit('TypeError: cannot read property of undefined')).toBe(false);
  });

  it('detects OpenAI API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'openai', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('RESOURCE_EXHAUSTED while calling model', 'gemini', 'service_account')).toBe(true);
  });

  it('detects Gemini API-key quota metric bodies', () => {
    const msg = "Quota exceeded for quota metric 'GenerateRequests'. Please retry in 39.8s.";
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini API-key billing quota bodies', () => {
    const msg = 'You exceeded your current quota, please check your plan and billing details.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini Code Assist fallback quota messages', () => {
    const msg = 'Automatically switching from gemini-2.5-pro. Switching to the gemini-2.5-flash model.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account Vertex quota bodies', () => {
    const msg = 'Resource exhausted. Please try again later. Please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('detects Gemini service-account quota metric bodies', () => {
    const msg = 'Quota exceeded for metric: aiplatform.googleapis.com/generate_content_requests';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('does not cross-match provider-specific rate limits', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'anthropic', 'oauth')).toBe(false);
  });

  it('detects "you\'ve hit your session limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your session limit · resets 7:10am (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" case-insensitively', () => {
    expect(matchesRateLimit("YOU'VE HIT YOUR SESSION LIMIT", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("you've hit your session limit · resets 7:10am (UTC)")).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)")).toBe(true);
  });

  it('detects structured api_error_status:429 in transcript regardless of provider', () => {
    const transcript = '{"type":"result","is_error":true,"api_error_status":429,"error":"rate_limit"}';
    expect(matchesRateLimit(transcript)).toBe(true);
    expect(matchesRateLimit(transcript, 'openai', 'api_key')).toBe(true);
  });

  it('does not match bare 429 without JSON key context', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });
});

describe('RateLimitError', () => {
  it('is an instance of RateLimitError and Error', () => {
    const err = new RateLimitError('Claude API usage exhausted — banner');
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe('RateLimitError');
    expect(err.message).toBe('Claude API usage exhausted — banner');
  });

  it('plain Error is not a RateLimitError', () => {
    const err = new Error('Claude API usage exhausted — banner');
    expect(err).not.toBeInstanceOf(RateLimitError);
  });
});

describe('provider runner prompt transport', () => {
  it('uses stdin for OpenAI run commands instead of argv prompts', () => {
    const runner = getProviderRunner('openai', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gpt-5');

    expect(command.promptInput).toBe('stdin');
    // Codex CLI invocation uses the canonical bypass flag at the legacy
    // fallback path — disables approval policy and sandbox in one knob,
    // equivalent to Claude's --dangerously-skip-permissions. Agent VMs
    // are ephemeral; sandboxing inside them stalls on approval prompts
    // nobody can answer. `--full-auto` is explicitly avoided since it
    // aliases --sandbox workspace-write and would override the bypass.
    // --json: emit JSONL events (added 2026-05-08) so the agent wrapper can
    // parse Codex tool calls and populate last_tool_use_at. Without it, every
    // Codex task is structurally invisible to the watchdog.
    expect(command.args).toEqual(['exec', '--skip-git-repo-check', '--json', '--dangerously-bypass-approvals-and-sandbox', '-m', 'gpt-5']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    expect(command.outputMode).toBe('codex-jsonl');
  });

  it('uses stdin for Gemini run commands instead of argv prompts', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gemini-2.5-pro');

    expect(command.promptInput).toBe('stdin');
    expect(command.args).toEqual(['--approval-mode=yolo', '-o', 'text', '-m', 'gemini-2.5-pro']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    // Sandbox disabled via env var since Gemini's approval-mode and
    // sandbox are separate knobs (YOLO auto-enables docker sandbox
    // unless GEMINI_SANDBOX=false pins it off).
    expect(command.env.GEMINI_SANDBOX).toBe('false');
    // Trust gate opt-out — ephemeral VM is the isolation boundary.
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildRunCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildRunCommand('Some prompt', 'gemini-2.5-pro');
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (api_key)', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });
});

// ── LLM_CLI_TRANSLATION parsing ───────────────────────────────────────────────
// The runtime consumes a JSON-encoded translation blob from the backend's
// settings-bag resolver. Validate parsing is tolerant of missing fields and
// fails-safe to null on malformed JSON so legacy dispatch still works.
describe('parseLLMCliTranslation', () => {
  const originalEnv = process.env.LLM_CLI_TRANSLATION;

  afterEach(() => {
    if (originalEnv === undefined) delete process.env.LLM_CLI_TRANSLATION;
    else process.env.LLM_CLI_TRANSLATION = originalEnv;
  });

  it('returns null when env var is absent', () => {
    delete process.env.LLM_CLI_TRANSLATION;
    expect(parseLLMCliTranslation()).toBeNull();
  });

  it('parses a valid translation', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      env: { FOO: 'bar' },
      args: ['--model', 'claude-opus-4-7', '--effort', 'high'],
      prompt_fragments: ['Plan carefully.'],
      unsupported: ['advisor.enabled (fallback: prompt fragment only)'],
    });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['--model', 'claude-opus-4-7', '--effort', 'high']);
    expect(parsed.env).toEqual({ FOO: 'bar' });
    expect(parsed.prompt_fragments).toEqual(['Plan carefully.']);
    expect(parsed.unsupported[0]).toMatch(/advisor/);
  });

  it('tolerates missing fields with safe defaults', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({ args: ['-m', 'gpt-5'] });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['-m', 'gpt-5']);
    expect(parsed.env).toEqual({});
    expect(parsed.prompt_fragments).toEqual([]);
    expect(parsed.unsupported).toEqual([]);
  });

  it('returns null on malformed JSON (fail-safe to legacy dispatch)', () => {
    process.env.LLM_CLI_TRANSLATION = '{not valid json';
    expect(parseLLMCliTranslation()).toBeNull();
  });
});

// ── captureTestBaseline timeout selection ─────────────────────────────────────
// execFileAsync passes its timeoutMs through to spawn's options dict (Node
// ignores it, so it doesn't affect runtime — see the comment in execFileAsync).
// That makes the configured timeout per runner observable here.
describe('captureTestBaseline timeout selection', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners containing scripts/test-backend.sh', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect((defined(bashCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners with docker in args', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'docker run --rm myimage pytest' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const dockerCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'docker');
    expect(dockerCall).toBeDefined();
    expect((defined(dockerCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses the passed timeoutMs (60s default) for non-Docker runners', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'node', test_cmd: 'npm test' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const npmCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'npm');
    expect(npmCall).toBeDefined();
    expect((defined(npmCall)[2] as { timeout: number }).timeout).toBe(60_000);
  });
});

// ── Auto-detect: scripts/test-backend.sh postgres flag follows Docker availability ─
// Without --postgres, test-backend.sh defaults to in-memory SQLite. CI runs
// Postgres. SQLite-passing-Postgres-failing fixes get pushed and only fail on
// CI, costing a full review-fix iteration. When Docker is available, auto-detect
// passes --postgres so the local gate matches CI. When Docker is absent, pass
// --no-postgres so tests still run via the in-memory SQLite path.
describe('detectTestRunners auto-detect postgres flag', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    vi.mocked(execSync).mockReset();
    // Default: Docker is available (execSync('docker version') succeeds)
    vi.mocked(execSync).mockReturnValue('' as unknown as ReturnType<typeof execSync>);
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
    vi.mocked(fs.existsSync).mockReset();
  });

  function fakeSelfRepoFs() {
    // Simulate Blueprint's own repo layout: backend/pytest.ini exists AND
    // scripts/test-backend.sh exists. No top-level package.json, no agent/
    // package.json — so the only auto-detected runner is the backend script.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) => {
      const s = String(p);
      return s.endsWith('/backend/pytest.ini') || s.endsWith('/scripts/test-backend.sh');
    });
  }

  it('appends --postgres when Docker is available (backend/pytest.ini + scripts/test-backend.sh)', async () => {
    fakeSelfRepoFs();
    // No buildConfig = auto-detect path
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('appends --postgres when Docker is available in testFixLoop', async () => {
    fakeSelfRepoFs();
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('runs targeted pytest selectors before impact coverage when requested', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' }],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, undefined, undefined,
      {
        targetPytestSelectors: ['backend/testing/test_models.py::test_create'],
        runImpactAfterTargeted: true,
        requireTestmonForImpact: true,
      },
    );

    const bashCalls = vi.mocked(spawn).mock.calls.filter(c => c[0] === 'bash');
    expect(bashCalls).toHaveLength(2);
    expect(bashCalls[0][1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
    expect(bashCalls[1][1]).toEqual(['scripts/test-backend.sh', '--postgres', '--require-testmon']);
  });

  it('does not scope away backend targeted runners when related files point elsewhere', async () => {
    const buildConfig = {
      directories: [
        { path: 'agent', type: 'node', test_cmd: 'npm test' },
        { path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' },
      ],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, ['agent/src/index.ts'], undefined,
      { targetPytestSelectors: ['backend/testing/test_models.py::test_create'] },
    );

    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect(defined(bashCall)[1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
  });

  it('appends --no-postgres when Docker is unavailable (captureTestBaseline)', async () => {
    fakeSelfRepoFs();
    // Simulate Docker absent: execSync throws
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });

  it('appends --no-postgres when Docker is unavailable in testFixLoop', async () => {
    fakeSelfRepoFs();
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });
});

// ── Agent reliability medium fixes ───────────────────────────────────────────
describe('execFileAsync process reliability', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not SIGKILL the original process group after the child exits during timeout cleanup', async () => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);
    let closeCb: ((code: number | null, signal: NodeJS.Signals | null) => void) | undefined;
    const proc = {
      pid: 4321,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
        if (event === 'close') closeCb = cb;
      }),
    };
    vi.mocked(spawn).mockReturnValue(proc as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('npm', ['test'], { timeoutMs: 1000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000);
    expect(killSpy).toHaveBeenCalledWith(-4321, 'SIGTERM');

    proc.exitCode = 143;
    await vi.advanceTimersByTimeAsync(5000);
    expect(killSpy).not.toHaveBeenCalledWith(-4321, 'SIGKILL');

    closeCb?.(143, 'SIGTERM');
    await promise;
    killSpy.mockRestore();
  });

  it('tags external SIGKILL as EOOM instead of ETIMEDOUT', async () => {
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 4322,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              cb(null, 'SIGKILL');
            });
          }
        }),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    await expect(execFileAsync('node', ['worker.js'], { timeoutMs: 60_000 }))
      .rejects.toMatchObject({ code: 'EOOM', signal: 'SIGKILL', killed: true });
  });
});

describe('provider runner reliability', () => {
  function mockProviderRun(stderrChunks: string[], code: number | null, signal: NodeJS.Signals | null = null) {
    vi.mocked(spawn).mockImplementation(() => {
      const stderrCbs: Array<(d: Buffer) => void> = [];
      const proc = {
        pid: 9876,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdin: { on: vi.fn(), end: vi.fn() },
        stdout: { on: vi.fn() },
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              for (const chunk of stderrChunks) {
                for (const stderrCb of stderrCbs) {
                  stderrCb(Buffer.from(chunk));
                }
              }
              proc.exitCode = code;
              proc.signalCode = signal;
              cb(code, signal);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
  }

  let savedTaskId: string | undefined;
  beforeEach(() => {
    vi.stubEnv('TASK_ID', '');
    vi.mocked(spawn).mockReset();
    vi.mocked(fs.writeFileSync).mockClear();
    vi.mocked(fs.chmodSync).mockClear();
    vi.mocked(fs.unlinkSync).mockClear();
    vi.mocked(fs.openSync).mockClear();
    vi.mocked(fs.openSync).mockReturnValue(3);
    savedTaskId = process.env.TASK_ID;
    delete process.env.TASK_ID;
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
    else delete process.env.TASK_ID;
  });

  it('uses the stderr tail so late rate-limit text is still classified', async () => {
    mockProviderRun(['x'.repeat(60_000), "\nyou've hit your usage limit\n"], 1);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('classifies clean-exit (code 0) with rate-limit text in tail as RateLimitError', async () => {
    // The Anthropic CLI sometimes emits the weekly-limit message as a normal
    // assistant turn then exits CLEAN (code 0); without this catch, every
    // handler treats the run as success and posts a degenerate artifact —
    // most recently the release_audit handler posted a `{audit_score: 0}`
    // stub that the qualifier scored as a legitimate 0.43 verdict instead
    // of routing through account rotation.
    mockProviderRun(["You've hit your weekly limit · resets May 29, 4am (UTC)\n"], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('clean-exit with rate-limit text >5KB from the tail does NOT match', async () => {
    // LOG_TAIL_BYTES = 5_000. Anything beyond that window from the end must
    // not match — a mid-conversation mention of the phrase (e.g. an earlier
    // tool result quoting documentation) is not a real rate-limit signal.
    // Chunks are concatenated to stderr in this mock; stderrBuffer is bounded
    // to its own tail by appendBoundedTail, so a leading rate-limit phrase
    // followed by 60KB of benign output drops the phrase out of the tail.
    mockProviderRun(["you've hit your usage limit\n", 'x'.repeat(60_000)], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('clean-exit with no rate-limit text resolves normally (regression)', async () => {
    mockProviderRun(['ordinary tool output\n'], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('writes the stdin prompt file as owner-only', async () => {
    vi.stubEnv('TASK_ID', '');
    mockProviderRun([], 0);

    await runProviderAgent('private prompt', null, { value: '' });

    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/agent_prompt_unknown.txt',
      'private prompt',
      { encoding: 'utf-8', mode: 0o600 },
    );
    expect(vi.mocked(fs.chmodSync)).toHaveBeenCalledWith('/tmp/agent_prompt_unknown.txt', 0o600);
  });
});

describe('stale result retry envelopes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('persists status payloads in a retry envelope before posting', async () => {
    vi.mocked(fs.writeFileSync).mockClear();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
    }));

    await persistAndPostStatus('task-1', { task_id: 'task-1', status: 'COMPLETED' });

    const writeCall = vi.mocked(fs.writeFileSync).mock.calls.find(call => call[0] === '/tmp/task_task-1_result.json');
    if (!writeCall) throw new Error('expected stale result file write');
    const rawWritten = writeCall[1];
    if (typeof rawWritten !== 'string') throw new Error('expected stale result write payload to be a string');
    const written = JSON.parse(rawWritten) as {
      payload: { status?: unknown };
      retry: { attempts?: unknown; nextAttemptAt?: unknown };
    };
    expect(written.payload.status).toBe('COMPLETED');
    expect(written.retry.attempts).toBe(0);
    expect(written.retry.nextAttemptAt).toBe(0);
  });

  it('parses legacy payload files and records per-file backoff after failures', () => {
    const legacy = parseStaleResultFile(JSON.stringify({ task_id: 'legacy-task', status: 'FAILED' }), 1000);
    expect(legacy.payload.task_id).toBe('legacy-task');
    expect(legacy.retry.createdAt).toBe(1000);

    const failed = markStaleResultFailure(legacy, new Error('network down'), 2000);
    expect(failed.retry.attempts).toBe(1);
    expect(failed.retry.nextAttemptAt).toBe(32_000);
    expect(failed.retry.lastError).toBe('network down');
  });

  it('abandons stale result files after the retry ceiling', () => {
    const stale = parseStaleResultFile(JSON.stringify({
      payload: { task_id: 'old-task' },
      retry: { attempts: 10, createdAt: 1000, nextAttemptAt: 0 },
    }), 1000);

    expect(shouldAbandonStaleResult(stale, 2000)).toBe(true);
  });

  it('fails final status with oauth_rotation_failed when OAuth rotation cannot persist', async () => {
    const oldProvider = process.env.PROVIDER;
    const oldAuthMode = process.env.AUTH_MODE;
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    vi.resetModules();
    vi.mocked(fs.existsSync).mockReturnValue(true);
    mockReadFileSync.mockImplementation((path, encoding) => {
      if (String(path).includes('/opt/agent/openai/auth.json')) {
        return encoding ? '{"refresh_token":"rotated"}' : Buffer.from('{"refresh_token":"rotated"}');
      }
      return '';
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/api/agent/credentials/rotate')) {
        return Promise.resolve({
          ok: false,
          status: 400,
          text: () => Promise.resolve('revoked token'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        } as unknown as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({ status: 'ok' }),
        headers: { get: () => null },
      } as unknown as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    try {
      const mod = await import('./index.js');
      await mod.persistAndPostStatus('task-oauth', {
        task_id: 'task-oauth',
        status: 'COMPLETED',
        logs: 'original completion',
      });
    } finally {
      process.env.PROVIDER = oldProvider;
      process.env.AUTH_MODE = oldAuthMode;
    }

    const statusCall = fetchMock.mock.calls.find(([url]) => {
      const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : url.url;
      return u.includes('/api/agent/status');
    });
    expect(statusCall).toBeTruthy();
    const rawBody = statusCall?.[1]?.body;
    const bodyStr = typeof rawBody === 'string' ? rawBody : '';
    const body = JSON.parse(bodyStr) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.oauth_rotation_failed).toBe(true);
    expect(body.failure_category).toBe('oauth_rotation_failed');
    expect(body.logs).toContain('OAuth credential rotation failed');
  });
});

// ── setupWorkspace: pre-baked git object cache reuse ─────────────────────────
describe('setupWorkspace reference-cache reuse', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(fs.existsSync).mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** execFileSync stub: ls-remote throws so the stale-branch path is skipped;
   *  every other git/rm/curl invocation succeeds. */
  function stubExecFileSyncNoStaleBranch(): void {
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (String(cmd) === 'git' && a[0] === 'ls-remote') {
        throw new Error('fatal: branch not found'); // → staleBranchExists = false
      }
      return '' as unknown as ReturnType<typeof execFileSync>;
    });
  }

  function findCloneArgs(): string[] {
    const cloneCall = vi.mocked(execFileSync).mock.calls.find(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[])[0] === 'clone',
    );
    expect(cloneCall).toBeDefined();
    return cloneCall?.[1] as string[];
  }

  it('clone args include --reference-if-able when the mirror exists', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true); // mirror present on disk
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' });

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).toContain('--reference-if-able');
    // Reference path matches GIT_REFERENCE_CACHE_ROOT/<owner>/<repo>.git.
    const refIdx = cloneArgs.indexOf('--reference-if-able');
    expect(cloneArgs[refIdx + 1]).toBe('/opt/agent-cache/git/owner/repo.git');
  });

  it('clone args exclude --reference-if-able when the mirror is absent', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false); // no mirror seeded for this repo
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' });

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).not.toContain('--reference-if-able');
  });
});

// ── Failure category classification ──────────────────────────────────────────
describe('failure category classification', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(spawn).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('setupWorkspace attaches clone_failed when git clone fails', async () => {
    vi.useFakeTimers();
    vi.mocked(execFileSync).mockImplementation((cmd: unknown) => {
      const cmdStr = String(cmd);
      // Allow rm (workspace cleanup) and curl (heartbeat) to succeed
      if (cmdStr === 'rm' || cmdStr === 'curl') return '' as unknown as ReturnType<typeof execFileSync>;
      // All git commands fail — simulates persistent clone failure across all retries
      throw new Error('fatal: Authentication failed');
    });

    let caughtError: unknown;
    const promise = setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' })
      .catch((e: unknown) => { caughtError = e; });

    // Advance past all retry delays (CLONE_MAX_RETRIES=3: 5s + 10s = 15s total)
    await vi.runAllTimersAsync();
    await promise;

    expect(caughtError).toBeInstanceOf(Error);
    expect((caughtError as Error & { failureCategory?: string }).failureCategory).toBe('clone_failed');
  });

  it('pushAndCreatePR attaches pr_creation_failed when GitHub PR creation returns 403', async () => {
    // Allow all execFileSync calls (git remote set-url, curl heartbeat)
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
    // Mock spawn to make gitAsync push succeed
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 123,
        stderr: { on: vi.fn() },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') setImmediate(() => { cb(0); });
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
    // First fetch: apiGet for fresh GitHub token; second fetch: apiPost for PR creation (403)
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: { token: 'fresh-token' } }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        text: () => Promise.resolve('Forbidden'),
      }),
    );

    let caughtError: unknown;
    try {
      // skipRebase=true avoids git fetch/rebase operations
      await pushAndCreatePR('owner', 'repo', 'task-id', 'blueprint/branch', 'main', 'Title', 'Body', null, { value: '' }, true);
    } catch (e) {
      caughtError = e;
    }

    expect(caughtError).toBeInstanceOf(Error);
    expect((caughtError as Error & { failureCategory?: string }).failureCategory).toBe('pr_creation_failed');
  });
});

// ── condensedAnalysisSections ────────────────────────────────────────────────
describe('condensedAnalysisSections', () => {
  it('extracts canonical headings exactly as written by the prompt', () => {
    const ctx = [
      '**Files to change**',
      '- a.ts',
      '',
      '**Interfaces to preserve**',
      '- Foo',
      '',
      '**Key dependencies**',
      '- bar.ts → a.ts',
      '',
      '**Callers to verify after implementation**',
      '- caller.ts (uses: foo)',
      '',
      '**Tests at risk of breaking**',
      '- testing/test_x.py — fixture change',
    ].join('\n');
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change**');
    expect(out).toContain('**Interfaces to preserve**');
    expect(out).toContain('**Key dependencies**');
    expect(out).toContain('**Callers to verify after implementation**');
    expect(out).toContain('**Tests at risk of breaking**');
    expect(out).toContain('- testing/test_x.py — fixture change');
  });

  it('extracts "Tests at risk of breaking" section', () => {
    const ctx = '**Tests at risk of breaking**\n- testing/test_foo.py — assertion change\n\n**Implementation approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Tests at risk of breaking**');
    expect(out).toContain('- testing/test_foo.py — assertion change');
    expect(out).not.toContain('Implementation approach');
  });

  it('matches case-insensitively', () => {
    const ctx = '**FILES TO CHANGE**\n- a.ts\n\n**Implementation Approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**FILES TO CHANGE**');
    expect(out).not.toContain('Implementation Approach');
  });

  it('matches "Files to modify" as a synonym for "Files to change"', () => {
    const ctx = '**Files to modify**\n- a.ts\n- b.ts';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to modify**');
    expect(out).toContain('- a.ts');
  });

  it('matches headings with parenthetical suffixes', () => {
    const ctx = '**Files to change** (in recommended order)\n- a.ts\n\n**Interfaces to preserve** (signatures)\n- Foo';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change** (in recommended order)');
    expect(out).toContain('**Interfaces to preserve** (signatures)');
  });

  it('matches "Key dependencies" or just "Dependencies"', () => {
    const ctx = '**Dependencies**\n- a → b';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Dependencies**');
    expect(out).toContain('- a → b');
  });

  it('matches "Callers to verify" with trailing variation', () => {
    const ctx = '**Callers to verify**\n- caller.ts';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Callers to verify**');
  });

  it('falls back to truncation when no key headings match', () => {
    const long = '**Risks**\n' + 'x'.repeat(1000);
    const out = condensedAnalysisSections(long);
    expect(out.length).toBeLessThanOrEqual(800);
    expect(out).toContain('**Risks**');
  });

  it('does not match unrelated headings that contain a single key term', () => {
    const ctx = '**Files used**\n- nope\n\n**Files to change**\n- a.ts\n\n**Implementation approach**\n- step';
    const out = condensedAnalysisSections(ctx);
    expect(out).toContain('**Files to change**');
    expect(out).not.toContain('**Files used**');
    expect(out).not.toContain('**Implementation approach**');
  });
});

// ── parseFailingTests ────────────────────────────────────────────────────────
describe('parseFailingTests', () => {
  it('parses pytest FAILED lines with full test ID and reason', () => {
    const output = [
      'FAILED backend/testing/test_foo.py::test_bar - AssertionError: expected 1 got 2',
      'FAILED backend/testing/test_baz.py::test_qux - ValueError',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'backend/testing/test_foo.py::test_bar',
      'backend/testing/test_baz.py::test_qux',
    ]));
  });

  it('parses vitest failure markers (✕, ✗, ×) with descriptions', () => {
    const output = [
      '✕ should render component correctly',
      '✗ handles empty input',
      '× rejects invalid payload',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'should render component correctly',
      'handles empty input',
      'rejects invalid payload',
    ]));
  });

  it('handles mixed pytest and vitest output in the same string', () => {
    const output = [
      'Running tests...',
      'FAILED backend/testing/test_foo.py::test_bar - AssertionError',
      '✕ frontend renders nav',
      'PASSED backend/testing/test_ok.py::test_ok',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set([
      'backend/testing/test_foo.py::test_bar',
      'frontend renders nav',
    ]));
  });

  it('returns an empty Set for output with no test failures', () => {
    const output = [
      'All tests passed!',
      'PASSED backend/testing/test_ok.py::test_ok',
      '✓ everything works',
      '',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.size).toBe(0);
  });

  it('handles pytest lines where the test ID contains special characters and parametrize brackets', () => {
    const output = [
      'FAILED backend/testing/test_p.py::test_param[case-1] - AssertionError',
      'FAILED backend/testing/test_p.py::test_unicode_ünîcødé - RuntimeError',
      'FAILED backend/testing/test_q.py::test_with_space description - fail',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.has('backend/testing/test_p.py::test_param[case-1]')).toBe(true);
    expect(result.has('backend/testing/test_p.py::test_unicode_ünîcødé')).toBe(true);
    expect(result.has('backend/testing/test_q.py::test_with_space description')).toBe(true);
  });

  it('parses vitest "FAIL  path > suite > test" stdout lines (default reporter)', () => {
    // Regression: prior parser missed vitest's " FAIL " format and
    // returned 0 failures for a real 125-failure vitest run on main —
    // which then leaked into the fix loop as "newly broken by the
    // agent's changes."
    const output = [
      ' FAIL  src/handlers/standard.test.ts > renders the right CLI name',
      ' FAIL  src/prompt.test.ts > appends DEFAULT_TOOL_GUIDANCE',
      ' PASS  src/index.test.ts > parses options',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.has('src/handlers/standard.test.ts > renders the right CLI name')).toBe(true);
    expect(result.has('src/prompt.test.ts > appends DEFAULT_TOOL_GUIDANCE')).toBe(true);
    expect(result.size).toBe(2);
  });

  it('does NOT match single-space "FAIL reason:" (test-output false positive)', () => {
    // A test that verifies failure-formatting logic can print lines like
    // `FAIL reason: expected X got Y` from its own stdout. Those must
    // not be treated as baseline failures. The tighter "FAIL + 2+
    // spaces" regex guards against this.
    const output = [
      'FAIL reason: expected true got false',
      'FAIL: some output from a passing test',
      ' FAIL something (only one trailing space)',
    ].join('\n');
    const result = parseFailingTests(output);
    expect(result.size).toBe(0);
  });

  it('matches vitest "⨯" (U+2A2F) marker in addition to ✕/✗/×', () => {
    const output = '⨯ some test failed';
    const result = parseFailingTests(output);
    expect(result).toEqual(new Set(['some test failed']));
  });
});

describe('pytest CI selector extraction', () => {
  it('extracts pytest selectors from CI FAILED lines and normalizes backend script args', () => {
    const output = [
      'FAILED backend/testing/test_models.py::test_create - AssertionError',
      'FAILED testing/test_pipeline.py::test_queue[case-1] - RuntimeError',
      'PASSED backend/testing/test_ok.py::test_ok',
    ].join('\n');

    const selectors = extractPytestSelectorsFromOutput(output);
    expect(selectors).toEqual([
      'backend/testing/test_models.py::test_create',
      'testing/test_pipeline.py::test_queue[case-1]',
    ]);

    const runner = {
      cmd: 'bash',
      args: ['scripts/test-backend.sh', '--postgres'],
      cwd: '/tmp/workspace',
      label: 'pytest (backend pyproject)',
    };
    expect(normalizePytestSelectorForRunner(selectors[0], runner)).toBe('testing/test_models.py::test_create');

    const directScriptRunner = {
      cmd: 'scripts/test-backend.sh',
      args: ['--postgres'],
      cwd: '/tmp/workspace',
      label: 'pytest (backend direct script)',
    };
    expect(normalizePytestSelectorForRunner(selectors[0], directScriptRunner)).toBe('testing/test_models.py::test_create');
  });

  it('builds targeted pytest runners by appending selectors only to pytest-compatible runners', () => {
    const runners = [
      { cmd: 'bash', args: ['scripts/test-backend.sh', '--postgres'], cwd: '/tmp/workspace', label: 'backend' },
      { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace/agent', label: 'agent' },
    ];

    const targeted = buildTargetedPytestRunners(runners, ['backend/testing/test_models.py::test_create']);

    expect(targeted).toHaveLength(1);
    expect(targeted[0].args).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
  });
});

describe('extractInvestigatedFiles', () => {
  it('extracts repo-relative paths from Read/Edit/Grep input previews and dedupes', () => {
    const trace = [
      { t: '1', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.tsx","offset":180,"limit":20}' },
      { t: '2', tool: 'Grep',  input_preview: '{"pattern":"total_operator_action","path":"/tmp/workspace/admin-ui/src","output_mode":"content"}' },
      { t: '3', tool: 'Edit',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.test.tsx","old_string":"x","new_string":"y"}' },
      { t: '4', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/admin-ui/src/components/DirectionCard.tsx","offset":200,"limit":40}' },
      { t: '5', tool: 'Bash',  input_preview: '{"command":"ls","description":"list"}' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual([
      'admin-ui/src/components/DirectionCard.tsx',
      'admin-ui/src',
      'admin-ui/src/components/DirectionCard.test.tsx',
    ]);
  });

  it('returns paths in first-seen order, preserves Grep directories, ignores non-investigation tools', () => {
    const trace = [
      { t: '1', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/backend/app/foo.py"}' },
      { t: '2', tool: 'Bash',  input_preview: '{"command":"echo hi"}' },
      { t: '3', tool: 'Grep',  input_preview: '{"pattern":"x","path":"/tmp/workspace/backend/app"}' },
      { t: '4', tool: 'Read',  input_preview: '{"file_path":"/tmp/workspace/backend/app/foo.py"}' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual([
      'backend/app/foo.py',
      'backend/app',
    ]);
  });

  it('handles JSON-truncated input previews via regex fallback', () => {
    const trace = [
      { t: '1', tool: 'Read', input_preview: '{"file_path":"/tmp/workspace/agent/src/handlers/review-fix.ts","offset":1,"limit":2000,"some_other_field":"...truncated' },
    ];
    expect(extractInvestigatedFiles(trace)).toEqual(['agent/src/handlers/review-fix.ts']);
  });
});

// ── parseFailingTestsFromVitestJson ─────────────────────────────────────────
describe('parseFailingTestsFromVitestJson', () => {
  it('extracts fullName of every failed assertion', () => {
    const json = JSON.stringify({
      testResults: [
        {
          name: '/path/foo.test.ts',
          assertionResults: [
            { fullName: 'describe A > does X', status: 'failed' },
            { fullName: 'describe A > does Y', status: 'passed' },
            { fullName: 'describe A > does Z', status: 'failed' },
          ],
        },
        {
          name: '/path/bar.test.ts',
          assertionResults: [
            { fullName: 'describe B > does W', status: 'failed' },
          ],
        },
      ],
    });
    expect(parseFailingTestsFromVitestJson(json)).toEqual(new Set([
      'describe A > does X',
      'describe A > does Z',
      'describe B > does W',
    ]));
  });

  it('falls back to title when fullName is missing', () => {
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          { title: 'legacy test', status: 'failed' },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json)).toEqual(new Set(['legacy test']));
  });

  it('ignores passed/skipped/pending assertions', () => {
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          { fullName: 'A', status: 'passed' },
          { fullName: 'B', status: 'skipped' },
          { fullName: 'C', status: 'pending' },
          { fullName: 'D', status: 'todo' },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json).size).toBe(0);
  });

  it('returns empty Set on malformed JSON', () => {
    expect(parseFailingTestsFromVitestJson('not json').size).toBe(0);
    expect(parseFailingTestsFromVitestJson('').size).toBe(0);
  });

  it('ignores simulated failure output in stdout — no false positives from mocked test bodies', () => {
    // This is the whole point: stdout from a passing vitest test whose body
    // prints something like "✕ simulated failure" would fool parseFailingTests.
    // The JSON reporter only records real failures.
    const json = JSON.stringify({
      testResults: [{
        assertionResults: [
          {
            fullName: 'testFixLoop > handles simulated failure output',
            status: 'passed',
          },
        ],
      }],
    });
    expect(parseFailingTestsFromVitestJson(json).size).toBe(0);
  });
});

// ── recordToolUse ────────────────────────────────────────────────────────────
describe('recordToolUse', () => {
  it('records a tool use entry with timestamp, tool name, and input preview', () => {
    recordToolUse('Bash', { command: 'ls' });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.tool).toBe('Bash');
    expect(entry.input_preview).toBe('{"command":"ls"}');
    expect(entry.t).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  it('truncates input_preview to 200 characters for long inputs', () => {
    recordToolUse('Bash', { command: 'x'.repeat(500) });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview.length).toBe(200);
  });

  it("records '<unserializable>' for inputs with circular references", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    recordToolUse('Bash', circular);
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('<unserializable>');
  });

  it('evicts oldest entry when buffer exceeds 20 entries (TOOL_TRACE_MAX)', () => {
    for (let i = 0; i < 25; i++) {
      recordToolUse(`tool-${i}`, { n: i });
    }
    const trace = snapshotToolTrace();
    expect(trace.length).toBe(20);
    const tools = trace.map(e => e.tool);
    expect(tools).toContain('tool-24');
    expect(tools).toContain('tool-5');
    expect(tools).not.toContain('tool-4');
    expect(tools).not.toContain('tool-0');
  });

  it('handles string input directly without JSON.stringify', () => {
    recordToolUse('Write', 'raw string value');
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('raw string value');
  });
});

// ── flushLogBuffer ───────────────────────────────────────────────────────────
describe('flushLogBuffer', () => {
  beforeEach(() => {
    // Drain any entries from prior tests by flushing into a no-op sink.
    const sink = { send: vi.fn() } as unknown as WebSocket;
    flushLogBuffer(sink);
  });

  it('sends all buffered entries and empties the buffer on success', () => {
    appendToLogBuffer('one');
    appendToLogBuffer('two');
    const ws = { send: vi.fn() } as unknown as WebSocket;
    flushLogBuffer(ws);
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(2);
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('retains the failing entry in the buffer when ws.send throws', () => {
    appendToLogBuffer('will-fail');
    appendToLogBuffer('never-attempted');
    const ws = { send: vi.fn(() => { throw new Error('socket closed'); }) } as unknown as WebSocket;
    flushLogBuffer(ws);
    const remaining = snapshotLogBuffer();
    expect(remaining.length).toBe(2);
    expect(remaining[0].content).toBe('will-fail');
    expect(remaining[1].content).toBe('never-attempted');
  });

  it('keeps only the unsent entries when send fails mid-flush', () => {
    appendToLogBuffer('a');
    appendToLogBuffer('b');
    appendToLogBuffer('c');
    let call = 0;
    const ws = { send: vi.fn(() => { call += 1; if (call === 2) throw new Error('fail'); }) } as unknown as WebSocket;
    flushLogBuffer(ws);
    const remaining = snapshotLogBuffer();
    expect(remaining.map(e => e.content)).toEqual(['b', 'c']);
  });
});

// ── Phase 0 telemetry — setup_secs / warm_start / prior_role_vm_id ───────────
describe('persistAndPostStatus telemetry enrichment', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
  });

  it('computes setup_secs from claim → first tool use gap', async () => {
    const claimAt = 1_000_000;
    _setRoleClaimTimestampForTesting(claimAt);
    // Simulate the SDK message loop calling recordToolUse 12.5s after claim.
    const dateNowSpy = vi.spyOn(Date, 'now').mockReturnValue(claimAt + 12_500);
    try {
      recordToolUse('Read', { file_path: '/x' });
      expect(_getFirstToolUseAtForTesting()).toBe(claimAt + 12_500);
    } finally {
      dateNowSpy.mockRestore();
    }

    await persistAndPostStatus('task-X', { task_id: 'task-X', status: 'COMPLETED' });
    expect(fetchMock).toHaveBeenCalled();
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number; warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.setup_secs).toBe(12.5);
  });

  it('warm_start defaults to false on initial role; null prior_role_vm_id', async () => {
    _setRoleClaimTimestampForTesting(2_000_000);
    await persistAndPostStatus('task-cold', { task_id: 'task-cold', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.warm_start).toBe(false);
    expect(body.prior_role_vm_id).toBeNull();
  });

  it('warm_start=true emits prior_role_vm_id from VM_INSTANCE_ID', async () => {
    const orig = process.env.VM_INSTANCE_ID;
    process.env.VM_INSTANCE_ID = 'agent-vm-warm-7';
    try {
      _setRoleClaimTimestampForTesting(3_000_000);
      _setWarmStartForTesting(true);
      await persistAndPostStatus('task-warm', { task_id: 'task-warm', status: 'COMPLETED' });
      const body = JSON.parse(
        (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
      ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
      expect(body.warm_start).toBe(true);
      expect(body.prior_role_vm_id).toBe('agent-vm-warm-7');
    } finally {
      if (orig === undefined) delete process.env.VM_INSTANCE_ID;
      else process.env.VM_INSTANCE_ID = orig;
    }
  });

  it('omits setup_secs when no tool use was recorded', async () => {
    _setRoleClaimTimestampForTesting(4_000_000);
    // No recordToolUse call — firstToolUseAt stays null.
    await persistAndPostStatus('task-quiet', { task_id: 'task-quiet', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number };
    expect(body.setup_secs).toBeUndefined();
  });

  it('enrichStatusPayload includes handler_duration_secs after the simulated handler-complete capture', async () => {
    // Mirrors what runRoleOnce does on the success path: capture
    // (Date.now() - roleClaimTimestamp)/1000 after the handler returns.
    // We seed the mutable directly via the test helper instead of running
    // the full role-loop.
    _setHandlerDurationForTesting(87.3);
    await persistAndPostStatus('task-h', { task_id: 'task-h', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBe(87.3);
  });

  it('omits handler_duration_secs when null (e.g. failure paths or pre-handler statuses)', async () => {
    // Default state — _setHandlerDurationForTesting(null) in beforeEach.
    await persistAndPostStatus('task-q', { task_id: 'task-q', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBeUndefined();
  });
});

// ── pollForNextRole ──────────────────────────────────────────────────────────
describe('pollForNextRole', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  // Spy on the global clearInterval; widening to any is the simplest path
  // around vi.spyOn's strict object/keyof typing for ambient globals.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let clearIntervalSpy: ReturnType<typeof vi.spyOn<any, any>>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    clearIntervalSpy = vi.spyOn(globalThis as any, 'clearInterval');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearIntervalSpy.mockRestore();
  });

  it('returns exit with poll_timeout when 204 forever', async () => {
    fetchMock.mockResolvedValue({ status: 204, json: () => Promise.resolve({}) });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-1',
      priorTaskId: 't-1',
      priorPrNumber: 42,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-tok',
      pollIntervalMs: 30,
      timeoutMs: 100,
      heartbeatIntervalMs: 50,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
    // vm-heartbeat must have fired at least once (initial).
    const heartbeatCalls = fetchMock.mock.calls.filter(c => String(c[0]).endsWith('/api/agent/vm-heartbeat'));
    expect(heartbeatCalls.length).toBeGreaterThanOrEqual(1);
    // Authorization header must be the X-Agent-Token (matches existing apiPost).
    const headers = (heartbeatCalls[0][1] as { headers?: Record<string, string> } | undefined)?.headers ?? {};
    expect(headers['X-Agent-Token']).toBe('jwt-tok');
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns claim with taskId on action=claim', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-X',
          warm_start: true,
          agent_token: 'rotated-jwt-for-next-X',
        }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // The freshly-minted JWT is forwarded so the role-loop can rotate
    // AGENT_TOKEN before the next /api/agent/claim call.
    expect(result).toEqual({
      action: 'claim',
      taskId: 'next-X',
      agentToken: 'rotated-jwt-for-next-X',
    });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns claim with undefined agentToken when backend omits it (deploy mismatch)', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // Old-shape claim response — no agent_token.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'claim', task_id: 'next-Y', warm_start: true }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // pollForNextRole exposes the missing-token case to the caller (the
    // role-loop) which raises reset_failed rather than silently
    // continuing with a stale prior-task JWT.
    expect(result).toEqual({ action: 'claim', taskId: 'next-Y' });
    expect(result.agentToken).toBeUndefined();
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns exit with reason on action=exit', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'another_vm_owns_this_pr' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-3',
      priorTaskId: 't-3',
      priorPrNumber: 7,
      apiBase: 'http://api.test',
      agentJwt: 'jwt3',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'another_vm_owns_this_pr' });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('clears heartbeat interval on every return path', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });
    const before = clearIntervalSpy.mock.calls.length;
    await pollForNextRole({
      vmInstanceId: 'vm-4',
      priorTaskId: 't-4',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt4',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(before);
  });

  it('treats non-204 non-200 responses as transient and keeps polling until timeout', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 503, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-5',
      priorTaskId: 't-5',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt5',
      pollIntervalMs: 20,
      timeoutMs: 80,
      heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
  });

  it('handles fetch network errors without crashing', async () => {
    let attempts = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      attempts += 1;
      if (attempts === 1) return Promise.reject(new Error('network failure'));
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'recovered' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-6',
      priorTaskId: 't-6',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt6',
      pollIntervalMs: 10,
      timeoutMs: 500,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'recovered' });
  });
});

// ── child-PID registration in runProviderAgent / execFileAsync ───────────────
describe('child PID registration', () => {
  beforeEach(() => {
    clearPidRegistry();
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('execFileAsync registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = { close?: (code: number, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 9999,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    // Right after spawn, before close — PID should be registered.
    expect(_getRegisteredPids()).toContain(9999);

    // Simulate clean exit so the close handler runs.
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids()).not.toContain(9999);
  });

  it('execFileAsync unregisters PID on error event', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 8888,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids()).toContain(8888);
    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(8888);
  });

  it('skips registration when child has no pid (spawn failure)', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: undefined,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids().length).toBe(0);
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids().length).toBe(0);
  });
});

// ── runProviderAgent PID register/unregister ─────────────────────────────────
// Issue 9 (partial): runProviderAgent uses spawn() under the hood; the close
// AND error event paths must each unregister so the reset routine doesn't
// chase a dead PID. Mirrors the execFileAsync coverage above but for the
// runner-spawn path. We mock fs.existsSync false so the ./note write is
// skipped (it's a no-op when WORKSPACE doesn't exist in this mock).
describe('runProviderAgent PID register/unregister', () => {
  beforeEach(() => {
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 4242,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    // Drain microtasks so the synchronous spawn-time registerChildPid runs.
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(4242);

    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(4242);
  });

  it('unregisters PID on error event before close fires', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 5252,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(5252);

    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(5252);
  });
});

// ── clearToolTrace — Issue 2 cross-role leak guard ───────────────────────────
describe('clearToolTrace', () => {
  it('drains the ring buffer so subsequent snapshotToolTrace returns []', () => {
    recordToolUse('Read', { file_path: '/tmp/a' });
    recordToolUse('Edit', { file_path: '/tmp/b' });
    expect(snapshotToolTrace().length).toBeGreaterThanOrEqual(2);

    clearToolTrace();

    expect(snapshotToolTrace()).toEqual([]);
    expect(extractInvestigatedFiles(snapshotToolTrace())).toEqual([]);
  });

  it('survives being called when the buffer is already empty', () => {
    clearToolTrace();
    clearToolTrace();
    expect(snapshotToolTrace()).toEqual([]);
  });

  it('after clear, new tool uses populate from a clean slate', () => {
    recordToolUse('Read', { file_path: '/before/file.ts' });
    clearToolTrace();
    recordToolUse('Read', { file_path: '/after/file.ts' });
    const files = extractInvestigatedFiles(snapshotToolTrace());
    expect(files).toEqual(['/after/file.ts']);
    expect(files).not.toContain('/before/file.ts');
  });
});

// ── pollForNextRole 401/403 handling — Issue 14 ──────────────────────────────
describe('pollForNextRole auth-revoked', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exits with reason=auth_revoked when next-role returns 401', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-401',
      priorTaskId: 't-401',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when next-role returns 403', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 403, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-403',
      priorTaskId: 't-403',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'forbidden-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when only the vm-heartbeat returns 401', async () => {
    // next-role keeps returning 204 forever; the heartbeat path detects
    // the 401 and the loop exits at the next iteration check. We use a
    // long timeout so we know exit comes from the auth flag, not poll_timeout.
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        // First heartbeat returns 401 — flips the authRevoked flag.
        return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-hb-401',
      priorTaskId: 't-hb',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
    // Sanity: heartbeat was attempted at least once (the initial fire).
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });
});

// ── getAgentHeaders — Issue 3 ───────────────────────────────────────────────
describe('getAgentHeaders', () => {
  it('returns headers with Content-Type and X-Agent-Token reflecting the live AGENT_TOKEN', () => {
    const headers = getAgentHeaders();
    expect(headers['Content-Type']).toBe('application/json');
    // The exact AGENT_TOKEN value depends on how the test harness was started
    // (process.env.AGENT_SECRET_TOKEN). The contract is just that the field
    // is present with a string value — token rotation is exercised by the
    // production tryRefreshToken path which reads back the same module-level
    // AGENT_TOKEN variable that getAgentHeaders reads.
    expect(typeof headers['X-Agent-Token']).toBe('string');
    expect(Object.keys(headers).sort()).toEqual(['Content-Type', 'X-Agent-Token']);
  });

  it('returns a fresh object each call (no shared mutable reference)', () => {
    // Callers shouldn't be able to mutate one returned headers object and
    // affect a subsequent call's headers. Object identity must differ.
    const h1 = getAgentHeaders();
    const h2 = getAgentHeaders();
    expect(h1).not.toBe(h2);
  });
});

// ── pollForNextRole heartbeat firing pattern — Issue 4 ──────────────────────
// Existing tests verify "heartbeat fired at least once". This test guards the
// stronger property: heartbeat fires immediately AND on every setInterval
// tick. A regression that drops the immediate fire (only triggers after
// heartbeatIntervalMs) would still satisfy ">= 1" but would be wrong.
describe('pollForNextRole vm-heartbeat timing', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fires once immediately and then on each heartbeat interval tick', async () => {
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // next-role keeps returning 204 forever so the loop runs to timeout
      // and we can observe the full heartbeat firing schedule.
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });

    // Real timers + a generous wall-clock window. Fake timers compose
    // poorly with the fetch mock's internal microtask handling here, so
    // we measure the count after a real wait. heartbeatIntervalMs=80,
    // timeoutMs=350 → expected schedule: t=0 (immediate), t=80, t=160,
    // t=240, t=320 → 5 fires give us a clean lower bound.
    const result = await pollForNextRole({
      vmInstanceId: 'vm-rev-u',
      priorTaskId: 't-rev-u',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-rev-u',
      pollIntervalMs: 25,
      timeoutMs: 350,
      heartbeatIntervalMs: 80,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');

    // The immediate fire alone gives us 1. Each setInterval tick at 80ms
    // adds another. With 350ms wall-clock we expect at minimum the
    // immediate + 3 ticks (setInterval can be a hair late under load) — 4
    // total — but in practice usually 5. Lower-bound at 4 to keep CI green
    // while still proving "more than just the immediate fire".
    expect(heartbeatCount).toBeGreaterThanOrEqual(4);
  });

  it('immediate vm-heartbeat fires synchronously before the first poll tick', async () => {
    // Structural property: the IIFE that does the immediate fire is called
    // on the synchronous portion of pollForNextRole's setup, so by the time
    // the first poll-tick await yields, fetchMock should already have one
    // heartbeat call in its queue. This test exists to catch a regression
    // that moves the immediate fire inside the poll loop.
    let heartbeatCount = 0;
    let nextRoleCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      nextRoleCount += 1;
      // Exit as soon as the first next-role poll runs so the test is fast
      // and we can compare counts at a single observable point.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    await pollForNextRole({
      vmInstanceId: 'vm-imm',
      priorTaskId: 't-imm',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-imm',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      // Make the interval-fire path practically inactive so any heartbeat
      // observed must have come from the immediate-fire IIFE.
      heartbeatIntervalMs: 60_000,
    });

    expect(nextRoleCount).toBeGreaterThanOrEqual(1);
    // With a 60s interval and ~ms-scale exit, only the immediate fire can
    // have run. Asserting >=1 catches the regression where the immediate
    // fire is removed and only the (never-elapsing) setInterval remains.
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });
});

// ── mid-stream tool-use stall watchdog ───────────────────────────────────────
// Verifies that a tool_use event with no subsequent tool-result (user event)
// triggers SIGTERM and ClaudeTimeoutError after TOOL_USE_STALL_TIMEOUT_MS.
describe('mid-stream stall watchdog', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeStallChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb as ChildHandlers['close'];
        else if (event === 'error') handlers.error = cb as ChildHandlers['error'];
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // Drive fake time forward in steps shorter than CLAUDE_STREAM_STALL_MS,
  // emitting a no-op 'system' event between steps. Each emission flows through
  // stdout.on('data') -> resetStallWatchdog(), keeping the stream-stall watchdog
  // alive so we can isolate the tool-use stall watchdog under test. 'system'
  // events do NOT mutate toolInFlight (only 'user' events do).
  const advanceWithStreamHeartbeat = async (
    totalMs: number,
    emitStdout: (s: string) => void,
  ) => {
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = totalMs;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(JSON.stringify({ type: 'system' }));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('emits SIGTERM and throws ClaudeTimeoutError when tool_use in-flight exceeds stall timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7701);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit a tool_use event — tool goes in-flight
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: '/tmp/x' } }] },
    }));

    // Advance past the stall threshold (watchdog checks every 60s)
    await vi.advanceTimersByTimeAsync(TOOL_USE_STALL_TIMEOUT_MS + 60_001);

    // Simulate the process exiting after SIGTERM
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');

    await expect(promise).rejects.toBeInstanceOf(ClaudeTimeoutError);
    expect(killSpy).toHaveBeenCalledWith(-7701, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('does not stall when tool-result (user event) arrives before timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7702);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use then tool result before stall timeout
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: {} }] },
    }));
    emitStdout(JSON.stringify({ type: 'user' }));

    // Advance well past the stall timeout — watchdog should not fire because
    // toolInFlight was cleared by the user event above. Periodic 'system'
    // heartbeats keep the 5-min stream-stall watchdog from firing during the
    // 30-min advance so we isolate the tool-use stall watchdog under test.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS + 60_001, emitStdout);

    // Process exits cleanly
    child.exitCode = 0;
    handlers.close?.(0, null);

    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7702, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('clears stall watchdog when process exits before threshold fires', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7703);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use but process exits before watchdog fires
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'ls' } }] },
    }));

    // Advance partially — under the tool-use stall threshold. Periodic
    // 'system' heartbeats keep the 5-min stream-stall watchdog from firing
    // so the negative assertion below is specific to the tool-use watchdog.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS - 60_000, emitStdout);

    child.exitCode = 0;
    handlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advance past threshold after close — watchdog should be cleared and not fire
    await vi.advanceTimersByTimeAsync(120_000);

    expect(killSpy).not.toHaveBeenCalledWith(-7703, 'SIGTERM');

    killSpy.mockRestore();
  });
});

// ── runProviderAgent stream-stall watchdog ────────────────────────────────────
// Verifies the stream-stall watchdog arms on spawn, resets on data chunks,
// and fires SIGTERM→SIGKILL when stdout goes silent for CLAUDE_STREAM_STALL_MS.
// The close/error handlers must clear the watchdog before it fires.
describe('runProviderAgent stream-stall watchdog', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('fires SIGTERM after CLAUDE_STREAM_STALL_MS with no stdout data', async () => {
    const { fakeChild, childHandlers } = makeChild(7001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Advance past stall threshold — watchdog should fire SIGTERM.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);

    expect(killSpy).toHaveBeenCalledWith(-7001, 'SIGTERM');

    // Clean up by resolving the promise.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('does NOT fire when stdout data resets the watchdog before threshold', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(7002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit data chunks just before the threshold, resetting the watchdog each time.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS - 100);
    emitData(Buffer.from('chunk1'));
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS - 100);
    emitData(Buffer.from('chunk2'));
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS - 100);

    // Should not have fired SIGTERM yet — each chunk reset the watchdog.
    expect(killSpy).not.toHaveBeenCalledWith(-7002, 'SIGTERM');

    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('clears watchdog in close handler so no kill fires after process exits', async () => {
    const { fakeChild, childHandlers } = makeChild(7003);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Close the process well before the stall threshold.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advancing past threshold should NOT fire the watchdog — it was cleared.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7003, 'SIGTERM');
    killSpy.mockRestore();
  });

  it('clears watchdog in error handler so no kill fires after process errors', async () => {
    const { fakeChild, childHandlers } = makeChild(7004);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    childHandlers.error?.(new Error('spawn error'));
    await promise.catch(() => undefined);

    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7004, 'SIGTERM');
    killSpy.mockRestore();
  });
});

// ── drainJsonChunks within-chunk yielding (silent-death v2) ───────────────────
// Verifies that drainJsonChunks yields to the event loop multiple times while
// processing a single large stream-json chunk, not just once per chunk. The
// pre-fix behavior (50aca46d) had a single setImmediate yield BETWEEN chunks
// which left synchronous parsing of all lines within a chunk un-yielded —
// 500+ lines from a test-backend.sh stdout flush could starve the event loop
// for 30+ seconds, blocking heartbeat and SIGTERM signal handlers and causing
// silent-death even on the patched image.
describe('drainJsonChunks within-chunk yielding', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler as ChildHandlers['close'];
        else if (event === 'error') childHandlers.error = handler as ChildHandlers['error'];
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('processes a 200-line single chunk correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit a 200-line chunk including a trailing 'result' event. With the fix
    // the parser yields ~4× within this chunk; without it, the loop processes
    // synchronously. Either way it must produce the same end state. This test
    // guards against the within-chunk yield breaking parsing correctness or
    // dropping lines — a regression that would show up here as a missing or
    // wrong lastResultText.
    const lines: string[] = [];
    for (let i = 0; i < 199; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-after-200-lines' }));
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Drain microtasks/immediates so drainJsonChunks completes fully.
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setImmediate(r));
    }

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-after-200-lines');
  });

  it('still completes parsing all lines correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Mix of types so we exercise the dispatch branches, including a 'result'
    // event that updates lastResultText (the only output that the caller
    // actually consumes).
    const lines: string[] = [];
    for (let i = 0; i < 120; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-result-after-120-ticks' }));
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Drain microtasks
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setImmediate(r));
    }

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-result-after-120-ticks');
  });
});

describe('fetchVmImageIdentity', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('parses image name and timestamp from GCP metadata path', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2-1730000000'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2-1730000000');
    expect(result.image_ts).toBe(1730000000);
  });

  it('returns null image_name when metadata service is unreachable', async () => {
    // Null (rather than 'unknown') so vm-heartbeat omits vm_image_name
    // and compute._vm_images doesn't get polluted with ('unknown', None)
    // entries that would skew get_stale_image_vms accounting.
    fetchMock.mockRejectedValue(new Error('network error'));
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_name when metadata returns non-ok status', async () => {
    fetchMock.mockResolvedValue({ ok: false, text: () => Promise.resolve('') });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_ts for image names without a numeric suffix', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2');
    expect(result.image_ts).toBeNull();
  });
});

describe('agent runtime fingerprint heartbeats', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const runtimeFingerprint = 'a'.repeat(64);

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    _setAgentRuntimeFingerprintForTesting(runtimeFingerprint);
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _setAgentRuntimeFingerprintForTesting(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('includes agent_runtime_fingerprint in /api/agent/vm-heartbeat payloads', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    const result = await pollForNextRole({
      vmInstanceId: 'vm-runtime-fp',
      priorTaskId: 'task-runtime-fp',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-runtime-fp',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 60_000,
    });

    expect(result).toEqual({ action: 'exit', reason: 'done' });
    const vmHeartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/vm-heartbeat'),
    );
    expect(vmHeartbeatCall).toBeDefined();
    const vmHeartbeatBody = (vmHeartbeatCall?.[1] as RequestInit | undefined)?.body;
    expect(typeof vmHeartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof vmHeartbeatBody === 'string' ? vmHeartbeatBody : undefined),
    ) as { agent_runtime_fingerprint?: string };
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
  });

  it('includes agent_runtime_fingerprint in /api/agent/heartbeat payloads', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    _startHeartbeatForTesting('task-runtime-fp', () => 'heartbeat-ok');
    await new Promise(resolve => setTimeout(resolve, 0));

    const heartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/heartbeat'),
    );
    expect(heartbeatCall).toBeDefined();
    const heartbeatBody = (heartbeatCall?.[1] as RequestInit | undefined)?.body;
    expect(typeof heartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof heartbeatBody === 'string' ? heartbeatBody : undefined),
    ) as { task_id?: string; agent_runtime_fingerprint?: string };
    expect(body.task_id).toBe('task-runtime-fp');
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
  });
});

// ── Per-task LLM telemetry (efficiency direction d-91d8dede) ────────────────
describe('LLM telemetry counters', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('starts at zero', () => {
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
    });
  });

  it('counts CLI invocations and accumulates tokens across calls', () => {
    recordLlmCallStart();
    recordLlmTokens(100, 50);
    recordLlmCallStart();
    recordLlmTokens(200, 75);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 2,
      total_input_tokens: 300,
      total_output_tokens: 125,
    });
  });

  it('ignores non-finite or negative token counts', () => {
    recordLlmTokens(Number.NaN, 50);
    recordLlmTokens(-5, -10);
    recordLlmTokens(100, Number.POSITIVE_INFINITY);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 100,
      total_output_tokens: 50,
    });
  });

  it('resets between roles so warm-VM continuity does not leak counts', () => {
    recordLlmCallStart();
    recordLlmTokens(500, 250);
    resetLlmTelemetry();
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
    });
  });
});

// ── Gemini text-mode token estimation ────────────────────────────────────────
describe('Gemini text-mode token estimation', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('estimateTokensFromChars returns ceil(chars/4)', () => {
    expect(estimateTokensFromChars(0)).toBe(0);
    expect(estimateTokensFromChars(1)).toBe(1);
    expect(estimateTokensFromChars(4)).toBe(1);
    expect(estimateTokensFromChars(5)).toBe(2);
    expect(estimateTokensFromChars(400)).toBe(100);
    expect(estimateTokensFromChars(1000)).toBe(250);
  });

  it('lands estimated tokens in the same getLlmTelemetry() fields Claude uses', () => {
    // Simulate what runProviderAgent does for outputMode='text': estimate from
    // prompt.length and lastResultText.length, then call recordLlmTokens.
    const promptChars = 2000;  // → 500 estimated input tokens
    const resultChars = 800;   // → 200 estimated output tokens
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(promptChars), estimateTokensFromChars(resultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBe(500);
    expect(telemetry.total_output_tokens).toBe(200);
    expect(telemetry.llm_call_count).toBe(1);
  });

  it('records non-zero tokens on partial output (timeout/kill scenario)', () => {
    // Even when the Gemini CLI is killed mid-run, lastResultText has partial
    // output. Verify estimation still produces non-zero values.
    const fullPromptChars = 5000;
    const partialResultChars = 100;
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(fullPromptChars), estimateTokensFromChars(partialResultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBeGreaterThan(0);
    expect(telemetry.total_output_tokens).toBeGreaterThan(0);
  });
});

// ── Cross-language classification contracts ───────────────────────────────────
// Assert that backend/app/failure_classification.py still contains the patterns
// that match what this agent emits. If one fails, update the matching side:
// either the string in agent/src/index.ts or the corresponding pattern in
// failure_classification._AGENT_LIVE_WORK_PATTERNS / _VIEWPORT_PATTERNS.
//
// Complements backend/testing/test_failure_classification.py
// TestCrossLanguageClassificationContracts, which checks the reverse direction
// (Python reads agent/src/index.ts and admin-ui/src/test/viewport.ts).
describe('cross-language classification contracts', () => {
  let classifierSrc = '';

  beforeAll(async () => {
    // vi.importActual bypasses the vi.mock('fs', ...) at the top of this file
    // so we can read real source files for the contract assertions.
    const realFs = await vi.importActual<typeof import('fs')>('fs');
    const nodePath = await import('node:path');
    const dir = new URL('.', import.meta.url).pathname;
    classifierSrc = realFs.readFileSync(
      nodePath.join(dir, '..', '..', 'backend', 'app', 'failure_classification.py'),
      'utf-8',
    ).toLowerCase();
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches PR-push heartbeat prefix', () => {
    // agent/src/index.ts emits: sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`)
    // where workBranch is a blueprint/ branch. If this fails, update either the
    // agent string or the classifier pattern so both stay in sync.
    expect(classifierSrc).toContain('"creating pull request for blueprint/"');
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches tool-call heartbeat prefix', () => {
    // agent/src/index.ts emits: statusRef.value = `Using tool: ${toolName}`
    // If this fails, update either the agent string or the classifier pattern.
    expect(classifierSrc).toContain('"using tool: "');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoOverflowAt function name', () => {
    // viewport.ts throws errors containing 'expectNoOverflowAt(...)'.
    // If this fails, update either the viewport helper name or the classifier pattern.
    expect(classifierSrc).toContain('"expectnooverflowat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoTextTruncationAt function name', () => {
    // viewport.ts throws errors containing 'expectNoTextTruncationAt(...)'.
    expect(classifierSrc).toContain('"expectnotexttruncationat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectMinTouchTargetAt function name', () => {
    // viewport.ts throws errors containing 'expectMinTouchTargetAt(...)'.
    expect(classifierSrc).toContain('"expectmintouchtargetat("');
  });
});

// ── codexStreamHasTurnCompleted (cause-C sentinel, 2026-05-26) ───────────────
// The Codex CLI emits `{"type":"turn.completed"}` only when the turn actually
// finished. Absent that event, exitCode=0 means the upstream provider died
// silently (token cap / refusal / 5xx the CLI swallowed). The memo handler
// reclassifies on this signal — see agent/src/handlers/memo.ts. These tests
// pin the pure parser the live drainer (drainCodexChunks) mirrors.
describe('codexStreamHasTurnCompleted', () => {
  it('returns false for a stream that started but never completed (cause C)', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('returns true for a stream containing a turn.completed event', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n'
      + '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
      + '{"type":"turn.completed","usage":{"input_tokens":42}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('ignores non-JSON noise (codex auth error log lines)', () => {
    const stream =
      '2026-05-26T10:00:00Z ERROR codex_login: token invalid\n'
      + '{"type":"thread.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('ignores malformed JSONL lines without crashing', () => {
    const stream =
      '{"type":"thread.started"}\n'
      + '{this is not valid json\n'
      + '{"type":"turn.completed"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('returns false on an empty stream', () => {
    expect(codexStreamHasTurnCompleted('')).toBe(false);
  });

  it('does not match substrings outside the type field', () => {
    // Defensive: a tool argument that mentions "turn.completed" must not
    // false-positive the sentinel. The check is gated on `obj.type` equality.
    const stream =
      '{"type":"item.completed","item":{"type":"agent_message","text":"the turn.completed event was missing"}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });
});
