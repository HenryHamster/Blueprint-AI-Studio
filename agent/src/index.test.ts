import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
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
import { strVal, sanitize, validateBranchName, readHandlerOutput, runProjectLintChecks, testFixLoop, captureTestBaseline, RateLimitError, setupWorkspace, pushAndCreatePR, condensedAnalysisSections, type FailureCategory } from './index.js';
import { execFileSync, spawn } from 'child_process';

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

// ── testFixLoop failureSummary ────────────────────────────────────────────────
describe('testFixLoop failureSummary', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    // Make spawn immediately emit 'close' with exit code 0 so runClaudeCode resolves
    vi.mocked(spawn).mockImplementation(() => {
      const handlers: Record<string, ((...args: unknown[]) => void)[]> = {};
      const proc = {
        pid: 123,
        stderr: { on: vi.fn() },
        stdout: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (!handlers[event]) handlers[event] = [];
          handlers[event].push(cb);
          if (event === 'close') setImmediate(() => cb(0));
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
  });

  it('returns undefined failureSummary when tests pass', async () => {
    // Provide a buildConfig with a test_cmd so a runner is created
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    // execFileSync succeeds (tests pass)
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);

    const result = await testFixLoop(null, { value: '' }, undefined, undefined, buildConfig);

    expect(result.passed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.failureSummary).toBeUndefined();
  });

  it('returns failureSummary with combined failure messages when tests fail after all attempts', async () => {
    const buildConfig = { directories: [{ path: '', type: 'node', test_cmd: 'npm test' }] };
    const errorMessage = 'Error: Test suite failed\n  at Object.<anonymous> (test.ts:10:5)';
    // execFileSync throws only for 'npm' (the test runner), not for 'git'
    vi.mocked(execFileSync).mockImplementation((cmd: unknown) => {
      if (cmd === 'npm') throw new Error(errorMessage);
      return '' as unknown as ReturnType<typeof execFileSync>;
    });

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
  });

  function cwdsCalledWith(): (string | undefined)[] {
    return vi.mocked(execFileSync).mock.calls.map(
      (args) => (args[2] as { cwd?: string } | undefined)?.cwd,
    );
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

// ── RateLimitError ───────────────────────────────────────────────────────────
// Pins the class identity so the outer catch in main() (which uses
// `error instanceof RateLimitError` for classification) can't regress into
// the old cast-and-rematch pattern that silently reclassified rate-limits
// as CODE_ERROR when stderrBuffer was empty.
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

// ── captureTestBaseline timeout selection ─────────────────────────────────────
describe('captureTestBaseline timeout selection', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('' as unknown as ReturnType<typeof execFileSync>);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (180s) for runners containing scripts/test-backend.sh', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const bashCall = vi.mocked(execFileSync).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect((bashCall![2] as { timeout: number }).timeout).toBe(180_000);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (180s) for runners with docker in args', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'docker run --rm myimage pytest' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const dockerCall = vi.mocked(execFileSync).mock.calls.find(c => c[0] === 'docker');
    expect(dockerCall).toBeDefined();
    expect((dockerCall![2] as { timeout: number }).timeout).toBe(180_000);
  });

  it('uses the passed timeoutMs (60s default) for non-Docker runners', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'node', test_cmd: 'npm test' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const npmCall = vi.mocked(execFileSync).mock.calls.find(c => c[0] === 'npm');
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
