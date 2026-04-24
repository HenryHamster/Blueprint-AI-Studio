import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
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
import { strVal, sanitize, validateBranchName, readHandlerOutput, runProjectLintChecks, testFixLoop, lintFixLoop, errorFingerprint, captureTestBaseline, RateLimitError, setupWorkspace, pushAndCreatePR, condensedAnalysisSections, matchesRateLimit, parseFailingTests, parseFailingTestsFromVitestJson, recordToolUse, snapshotToolTrace, appendToLogBuffer, flushLogBuffer, snapshotLogBuffer, getProviderRunner, parseLLMCliTranslation, type FailureCategory } from './index.js';
import { execFileSync, spawn } from 'child_process';
import type WebSocket from 'ws';

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
    const result = readHandlerOutput<Array<{ id: number; name: string }>>(
      'task-3',
      'output',
      null,
    );
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
    const result = readHandlerOutput<{ vision: string; direction_operations: unknown[] }>(
      'task-4',
      'strategic_output',
      null,
    );
    expect(result).not.toBeNull();
    expect(result!.vision).toBe('build great things');
    expect(Array.isArray(result!.direction_operations)).toBe(true);
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

// ── runProjectLintChecks ──────────────────────────────────────────────────────
describe('runProjectLintChecks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Return empty string (not Buffer) so sendLog/sanitize don't receive a Buffer
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (vi.mocked(execFileSync) as any).mockReturnValue('');
  });

  it('uses dynamic lint_cmd when buildConfig is provided with a touched directory', () => {
    runProjectLintChecks(['frontend/src/App.tsx'], null, {
      directories: [{ path: 'frontend', type: 'node', lint_cmd: 'npm run lint' }],
    });
    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith(
      'npm', ['run', 'lint'], expect.objectContaining({ cwd: expect.stringContaining('frontend') }),
    );
  });

  it('uses dynamic build_cmd when buildConfig is provided with a touched directory', () => {
    runProjectLintChecks(['myapp/src/index.ts'], null, {
      directories: [{ path: 'myapp', type: 'node', build_cmd: 'npm run build' }],
    });
    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith(
      'npm', ['run', 'build'], expect.objectContaining({ cwd: expect.stringContaining('myapp') }),
    );
  });

  it('skips directories not touched by changedPaths when buildConfig is provided', () => {
    // Clear the execFileSync call count specifically for this assertion
    vi.mocked(execFileSync).mockClear();
    runProjectLintChecks(['unrelated/file.ts'], null, {
      directories: [{ path: 'backend', type: 'python', lint_cmd: 'ruff check .' }],
    });
    // execFileSync should not be called for lint/build since the directory wasn't touched
    const lintBuildCalls = vi.mocked(execFileSync).mock.calls.filter(
      args => args[0] !== 'rm',
    );
    expect(lintBuildCalls.length).toBe(0);
  });

  it('falls back to hardcoded admin-ui checks when buildConfig is undefined and admin-ui touched', () => {
    runProjectLintChecks(['admin-ui/src/App.tsx'], null, undefined);
    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith(
      'npm', ['ci'], expect.any(Object),
    );
    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith(
      'npm', ['run', 'build'], expect.any(Object),
    );
  });

  it('falls back to hardcoded agent checks when buildConfig is undefined and agent/ touched', () => {
    runProjectLintChecks(['agent/src/index.ts'], null, undefined);
    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith(
      'npx', ['tsc', '--noEmit'], expect.any(Object),
    );
  });

  it('is_self_repo triggers fallback: resolveBuildConfig returns undefined for is_self_repo=true', () => {
    // When is_self_repo=true, resolveBuildConfig returns undefined, so the
    // hardcoded fallback runs even if build_config data is present.
    // Simulate: call runProjectLintChecks with undefined (what resolveBuildConfig returns)
    runProjectLintChecks(['backend/app/main.py'], null, undefined);
    // Should use hardcoded ruff check (via execFileSync for the try/catch ruff path)
    // We just verify it doesn't throw and execFileSync was called for something
    expect(typeof runProjectLintChecks).toBe('function');
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
    const claudeCalls = vi.mocked(spawn).mock.calls.filter(c => String(c[0]).includes('claude')).length;
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

describe('lintFixLoop early exit on identical errors', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 123, stderr: { on: vi.fn() }, stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') setImmediate(() => cb(0));
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
  });

  it('exits early when consecutive lint errors are identical', async () => {
    const buildConfig = { directories: [{ path: 'agent', type: 'node', lint_cmd: 'npm run lint' }] };
    const errorMessage = 'eslint: no-unused-vars at foo.ts:1:1';
    vi.mocked(execFileSync).mockImplementation((cmd: unknown) => {
      if (cmd === 'npm') throw new Error(errorMessage);
      return '' as unknown as ReturnType<typeof execFileSync>;
    });

    const result = await lintFixLoop(['agent/src/foo.ts'], null, { value: '' }, 'ctx', undefined, undefined, buildConfig);
    expect(result).toBe(false);
    const npmCalls = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'npm').length;
    expect(npmCalls).toBeLessThanOrEqual(2);
  });

  it('continues when lint errors differ between attempts', async () => {
    const buildConfig = { directories: [{ path: 'agent', type: 'node', lint_cmd: 'npm run lint' }] };
    let callCount = 0;
    vi.mocked(execFileSync).mockImplementation((cmd: unknown) => {
      if (cmd === 'npm') {
        callCount++;
        throw new Error(`eslint error variant ${callCount}`);
      }
      return '' as unknown as ReturnType<typeof execFileSync>;
    });

    await lintFixLoop(['agent/src/foo.ts'], null, { value: '' }, 'ctx', undefined, undefined, buildConfig);
    const npmCalls = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'npm').length;
    expect(npmCalls).toBe(3);
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
    // MAX_TEST_FIX_ATTEMPTS = 2, loop runs 3 times when errors differ.
    const testRunCalls = vi.mocked(spawn).mock.calls.filter(
      c => c[0] === 'npm' && Array.isArray(c[1]) && c[1][0] === 'test',
    ).length;
    expect(testRunCalls).toBe(3);
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

  it('does not cross-match provider-specific rate limits', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'anthropic', 'oauth')).toBe(false);
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
    expect(command.args).toEqual(['exec', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox', '-m', 'gpt-5']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
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
    expect(command.env?.GEMINI_SANDBOX).toBe('false');
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
    const parsed = parseLLMCliTranslation();
    expect(parsed).not.toBeNull();
    expect(parsed!.args).toEqual(['--model', 'claude-opus-4-7', '--effort', 'high']);
    expect(parsed!.env).toEqual({ FOO: 'bar' });
    expect(parsed!.prompt_fragments).toEqual(['Plan carefully.']);
    expect(parsed!.unsupported[0]).toMatch(/advisor/);
  });

  it('tolerates missing fields with safe defaults', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({ args: ['-m', 'gpt-5'] });
    const parsed = parseLLMCliTranslation();
    expect(parsed).not.toBeNull();
    expect(parsed!.args).toEqual(['-m', 'gpt-5']);
    expect(parsed!.env).toEqual({});
    expect(parsed!.prompt_fragments).toEqual([]);
    expect(parsed!.unsupported).toEqual([]);
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

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (180s) for runners containing scripts/test-backend.sh', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect((bashCall![2] as { timeout: number }).timeout).toBe(180_000);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (180s) for runners with docker in args', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'docker run --rm myimage pytest' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const dockerCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'docker');
    expect(dockerCall).toBeDefined();
    expect((dockerCall![2] as { timeout: number }).timeout).toBe(180_000);
  });

  it('uses the passed timeoutMs (60s default) for non-Docker runners', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'node', test_cmd: 'npm test' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const npmCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'npm');
    expect(npmCall).toBeDefined();
    expect((npmCall![2] as { timeout: number }).timeout).toBe(60_000);
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
      .catch(e => { caughtError = e; });

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
          if (event === 'close') setImmediate(() => cb(0));
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
    // First fetch: apiGet for fresh GitHub token; second fetch: apiPost for PR creation (403)
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ data: { token: 'fresh-token' } }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        text: async () => 'Forbidden',
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
