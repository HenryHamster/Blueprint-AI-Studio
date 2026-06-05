import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock the IO surface of ../index.js so testFixLoop runs without spawning real
// processes or touching the network. execFileAsync is the runner-invocation
// seam we control per-test to simulate placeholder / empty / real suites.
// vi.hoisted so the fn is initialized before the hoisted vi.mock factory runs.
const { execFileAsync } = vi.hoisted(() => ({
  execFileAsync: vi.fn<(cmd: string, args: string[], opts?: unknown) => Promise<string>>(),
}));
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  sendHeartbeatSync: vi.fn(),
  execFileAsync,
  errorFingerprint: (s: string) => s.slice(0, 64),
  git: vi.fn(() => ''),
}));

// runClaudeCode must never be invoked for maxFixAttempts:0 runs — stub anyway.
vi.mock('../providers/runners.js', () => ({
  runClaudeCode: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../fix-loop-events.js', () => ({
  pushFixLoopEvent: vi.fn(),
  fingerprintHash: (s: string) => s,
}));

vi.mock('./diff-detection.js', () => ({
  captureDiffLines: vi.fn(() => null),
  detectLoopDivergence: vi.fn(() => null),
}));

vi.mock('child_process', () => ({
  execSync: vi.fn(() => ''),
}));

vi.mock('fs', () => ({
  default: {
    // node_modules present → skip npm ci; no pre-install marker files present
    // so Python/Go/Rust pre-install steps are not triggered in these tests.
    existsSync: vi.fn((p: unknown) =>
      typeof p === 'string' && (p.endsWith('/node_modules') || p.includes('/node_modules/')),
    ),
    readFileSync: vi.fn(() => ''),
    unlinkSync: vi.fn(),
  },
}));

import fs from 'fs';
import { testFixLoop, matchesSizeBreachSignature } from './test-fix-loop.js';
import { runClaudeCode } from '../providers/runners.js';
import type { TestRunner } from './test-runners.js';

// Default fs.existsSync behavior matching the module-level mock factory:
// only node_modules paths "exist" (so npm ci is skipped and no non-npm
// pre-install marker files are present). Tests that exercise a specific
// ecosystem's pre-install path override this per-test.
const defaultExistsSync = (p: unknown): boolean =>
  typeof p === 'string' && (p.endsWith('/node_modules') || p.includes('/node_modules/'));

const NPM_RUNNER: TestRunner = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' };
const PLACEHOLDER_RUNNER: TestRunner = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace/packages/a', label: 'npm test (packages/a)' };
const REAL_RUNNER: TestRunner = { cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'], cwd: '/tmp/workspace/packages/b', label: 'pytest (packages/b)' };
const statusRef = { value: '' };

describe('testFixLoop — noTestsExecuted aggregation (maxFixAttempts: 0)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('placeholder command that exits non-zero → noTestsExecuted=true', async () => {
    // npm "no test specified" placeholder: exits 1 with a no-tests signature.
    execFileAsync.mockRejectedValue(
      Object.assign(new Error('Command failed: npm test\nError: no test specified\nnpm ERR! Test failed.'), {}),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.noTestsExecuted).toBe(true);
  });

  it('empty suite that exits 0 → passed but noTestsExecuted=true (not a vacuous pass)', async () => {
    execFileAsync.mockResolvedValue('collected 0 items\n\nno tests ran in 0.01s');

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(true);
    expect(result.noTestsExecuted).toBe(true);
  });

  it('Go empty-package output → passed but noTestsExecuted=true', async () => {
    execFileAsync.mockResolvedValue('?    example.com/foo [no test files]');

    const goRunner: TestRunner = { cmd: 'go', args: ['test', './...'], cwd: '/tmp/workspace', label: 'go test (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [goRunner] },
    );

    expect(result.passed).toBe(true);
    expect(result.noTestsExecuted).toBe(true);
  });

  it('Rust zero-test summary → passed but noTestsExecuted=true', async () => {
    execFileAsync.mockResolvedValue([
      'running 0 tests',
      '',
      'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s',
    ].join('\n'));

    const cargoRunner: TestRunner = { cmd: 'cargo', args: ['test'], cwd: '/tmp/workspace', label: 'cargo test (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [cargoRunner] },
    );

    expect(result.passed).toBe(true);
    expect(result.noTestsExecuted).toBe(true);
  });

  it('a genuine test failure that mentions an empty-suite phrase → noTestsExecuted is NOT set', async () => {
    execFileAsync.mockRejectedValue(
      new Error('Command failed: npm test\nFAILED src/foo.test.ts > test_command_parser\nAssertionError: collected 0 items but 1 test failed'),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.noTestsExecuted).toBe(false);
  });

  it('real failure quoting a placeholder phrase as data → noTestsExecuted=false, failure stays reportable', async () => {
    // The suite genuinely ran and a test failed; its assertion text merely
    // QUOTES the npm placeholder literal "no test specified". A whole-output
    // substring classifier would misread this as an empty suite and drop the
    // failure, failing the no-CI gate open. The anchored classifier keeps it
    // a real run so the failure blocks the merge.
    execFileAsync.mockRejectedValue(
      new Error("Command failed: python3 -m pytest -x -q\nFAILED test_cli.py::test_help - AssertionError: expected 'no test specified' in output\n1 failed in 0.30s"),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [REAL_RUNNER] },
    );

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.noTestsExecuted).toBe(false);
    expect(result.failureSummary).toContain('AssertionError');
  });

  it('a genuine passing suite → noTestsExecuted is NOT set', async () => {
    execFileAsync.mockResolvedValue('Tests  3 passed (3)\nTest Files  1 passed (1)');

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(true);
    expect(result.noTestsExecuted).toBe(false);
  });

  it('mixed real-pass + placeholder runner → placeholder is ignored and verdict stays green', async () => {
    execFileAsync
      .mockRejectedValueOnce(Object.assign(new Error('Command failed: npm test\nError: no test specified\nnpm ERR! Test failed.'), {}))
      .mockResolvedValueOnce('Tests  3 passed (3)\nTest Files  1 passed (1)');

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [PLACEHOLDER_RUNNER, REAL_RUNNER] },
    );

    expect(result.passed).toBe(true);
    expect(result.testFailuresDetected).toBe(false);
    expect(result.noTestsExecuted).toBe(false);
  });

  it('mixed real-fail + placeholder runner where the real failure mentions no-tests text → real failure still blocks', async () => {
    execFileAsync
      .mockRejectedValueOnce(new Error('Command failed: python3 -m pytest -x -q\nFAILED src/foo.test.ts > test_command_parser\nAssertionError: no tests ran but 1 assertion failed'))
      .mockRejectedValueOnce(Object.assign(new Error('Command failed: npm test\nError: no test specified\nnpm ERR! Test failed.'), {}));

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [REAL_RUNNER, PLACEHOLDER_RUNNER] },
    );

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.noTestsExecuted).toBe(false);
    expect(result.failureSummary).toContain('AssertionError');
    expect(result.failureSummary).not.toContain('no test specified');
  });

  it('all-placeholder multi-runner → noTestsExecuted=true so the backend can fall back', async () => {
    execFileAsync
      .mockRejectedValueOnce(Object.assign(new Error('Command failed: npm test\nError: no test specified\nnpm ERR! Test failed.'), {}))
      .mockRejectedValueOnce(Object.assign(new Error('Command failed: npm test\nError: no test specified\nnpm ERR! Test failed.'), {}));

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [PLACEHOLDER_RUNNER, { ...PLACEHOLDER_RUNNER, cwd: '/tmp/workspace/packages/c', label: 'npm test (packages/c)' }] },
    );

    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
    expect(result.noTestsExecuted).toBe(true);
  });
});

// The structured assertion test_invariants._size_breach_message emits, verbatim
// in shape (path + line count + limit + the module_size_limits.json pointer).
const SIZE_BREACH_FAILURE =
  'Command failed: npm test\n' +
  'AssertionError: prompt.ts has 1700 lines (limit: 1650).\n\n' +
  'If your change legitimately needed to grow this file:\n' +
  '  2. Apply the new_limit returned in the response to\n' +
  "     backend/testing/module_size_limits.json under 'agent'.";

describe('matchesSizeBreachSignature', () => {
  it('matches the test_invariants size-breach assertion', () => {
    expect(matchesSizeBreachSignature(SIZE_BREACH_FAILURE)).toBe(true);
  });

  it('matches the fast local checker breach line (cap: N) form', () => {
    const checkerOutput =
      '[breach] agent/prompt.ts: 1700 lines (cap: 1650)\n\n' +
      '1 size breach(es). Run with --fix to bump the breached caps.';
    expect(matchesSizeBreachSignature(checkerOutput)).toBe(true);
  });

  it('matches the missing-cap coverage failure (threshold + NO cap entry)', () => {
    // test_*_no_uncapped_large_file emits this shape; its remediation is to ADD
    // a cap, which check_size_limits.py --fix now does deterministically.
    const coverageFailure =
      '1 source file(s) exceed 1500 lines with NO cap entry under \'agent\' in\n' +
      'backend/testing/module_size_limits.json:\n\n' +
      '  - prompts/huge.ts has 1700 lines (threshold: 1500)\n';
    expect(matchesSizeBreachSignature(coverageFailure)).toBe(true);
  });

  it('matches the local checker [uncapped] line form', () => {
    const checkerOutput =
      '[uncapped] agent/prompts/huge.ts: 1700 lines (threshold: 1500) — NO cap entry\n\n' +
      '1 uncapped file(s) over 1500 lines violate the size guardrail (no cap entry).';
    expect(matchesSizeBreachSignature(checkerOutput)).toBe(true);
  });

  it('does NOT match an ordinary assertion failure', () => {
    expect(
      matchesSizeBreachSignature('AssertionError: expected 1 to equal 2'),
    ).toBe(false);
  });

  it('does NOT match a numeric "lines (limit: N)" mention without a size anchor', () => {
    // The numeric form alone (no module_size_limits.json / guardrail / breach
    // anchor) must not trigger a cap bump.
    expect(
      matchesSizeBreachSignature('file has 12 lines (limit: 5) of diff context'),
    ).toBe(false);
  });

  it('does NOT match a "threshold" mention without a size anchor', () => {
    // A bare "(threshold: N)" with no module_size_limits.json / guardrail anchor
    // must not be mistaken for a missing-cap coverage failure.
    expect(
      matchesSizeBreachSignature('retry budget has 3 lines (threshold: 5) remaining'),
    ).toBe(false);
  });
});

describe('testFixLoop — deterministic size-breach remediation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('auto-bumps the cap via check_size_limits.py --fix and re-verifies — no LLM call', async () => {
    let testRunCalls = 0;
    execFileAsync.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'python3' && args.includes('--fix')) {
        return Promise.resolve('[fix]  agent/prompt.ts: 1700 lines — cap 1650 -> 1900\nBumped 1 cap(s).');
      }
      // Test runner: first run reports the size breach, the re-run (after the
      // cap bump) is clean.
      testRunCalls += 1;
      if (testRunCalls === 1) return Promise.reject(new Error(SIZE_BREACH_FAILURE));
      return Promise.resolve('Tests  3 passed (3)');
    });

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 1, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(true);
    // The canonical checker was invoked with the --fix contract.
    const bumpCall = execFileAsync.mock.calls.find(
      ([cmd, args]) => cmd === 'python3' && Array.isArray(args) && args.includes('--fix'),
    );
    expect(bumpCall).toBeDefined();
    expect(bumpCall?.[1]).toEqual(['backend/testing/check_size_limits.py', '--fix']);
    // The breach was resolved deterministically — no refactor/LLM path taken.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });

  it('auto-adds a missing cap for an uncapped large file — no LLM call', async () => {
    const COVERAGE_FAILURE =
      'Command failed: npm test\n' +
      '1 source file(s) exceed 1500 lines with NO cap entry under \'agent\' in\n' +
      'backend/testing/module_size_limits.json:\n\n' +
      '  - prompts/huge.ts has 1700 lines (threshold: 1500)\n';
    let testRunCalls = 0;
    execFileAsync.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'python3' && args.includes('--fix')) {
        return Promise.resolve(
          '[add]  agent/prompts/huge.ts: 1700 lines — added cap 1900\nAdded 1 cap(s).',
        );
      }
      testRunCalls += 1;
      if (testRunCalls === 1) return Promise.reject(new Error(COVERAGE_FAILURE));
      return Promise.resolve('Tests  3 passed (3)');
    });

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 1, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.passed).toBe(true);
    const bumpCall = execFileAsync.mock.calls.find(
      ([cmd, args]) => cmd === 'python3' && Array.isArray(args) && args.includes('--fix'),
    );
    expect(bumpCall?.[1]).toEqual(['backend/testing/check_size_limits.py', '--fix']);
    // Remediated deterministically — the refactor-biased LLM path never ran.
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
  });

  it('a non-size failure still routes to the Claude fix path (no cap bump)', async () => {
    execFileAsync.mockRejectedValue(
      new Error('Command failed: npm test\nAssertionError: expected 1 to equal 2'),
    );

    await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 1, runnersOverride: [NPM_RUNNER] },
    );

    // The LLM fix path runs; the deterministic checker is never invoked.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
    const bumpCall = execFileAsync.mock.calls.find(
      ([cmd]) => cmd === 'python3',
    );
    expect(bumpCall).toBeUndefined();
  });
});

describe('testFixLoop — non-npm pre-test dependency install (maxFixAttempts: 0)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // clearAllMocks does not reset implementations; restore the default so a
    // prior test's per-ecosystem override never leaks into this one.
    vi.mocked(fs.existsSync).mockImplementation(defaultExistsSync);
  });

  it('Python runner + poetry.lock → `poetry install --no-root` runs, then the suite runs and passes', async () => {
    vi.mocked(fs.existsSync).mockImplementation(
      (p: unknown) => typeof p === 'string' && p.endsWith('/poetry.lock'),
    );
    execFileAsync.mockImplementation((cmd: string) =>
      cmd === 'poetry'
        ? Promise.resolve('Installing dependencies from lock file')
        : Promise.resolve('Tests  3 passed (3)'),
    );

    const pyRunner: TestRunner = { cmd: 'pytest', args: ['-q'], cwd: '/tmp/workspace', label: 'pytest (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [pyRunner] },
    );

    const poetryCall = execFileAsync.mock.calls.find(([cmd]) => cmd === 'poetry');
    expect(poetryCall?.[1]).toEqual(['install', '--no-root']);
    // The suite itself ran after a successful install.
    expect(execFileAsync.mock.calls.some(([cmd]) => cmd === 'pytest')).toBe(true);
    expect(result.passed).toBe(true);
    expect(result.setupFailed).toBeFalsy();
  });

  it('Python runner + requirements.txt and a failing pip install → setupErrors → setupFailed (not a code-fix failure)', async () => {
    vi.mocked(fs.existsSync).mockImplementation(
      (p: unknown) => typeof p === 'string' && p.endsWith('/requirements.txt'),
    );
    execFileAsync.mockImplementation((cmd: string) =>
      cmd === 'pip'
        ? Promise.reject(new Error('Command failed: pip install -r requirements.txt\nERROR: Could not find a version that satisfies the requirement foo'))
        : Promise.resolve('Tests  3 passed (3)'),
    );

    const pyRunner: TestRunner = { cmd: 'python3', args: ['-m', 'pytest'], cwd: '/tmp/workspace', label: 'pytest (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [pyRunner] },
    );

    const pipCall = execFileAsync.mock.calls.find(([cmd]) => cmd === 'pip');
    expect(pipCall?.[1]).toEqual(['install', '-r', 'requirements.txt']);
    // The suite was skipped after the install failure — the runner never ran.
    expect(execFileAsync.mock.calls.some(([cmd]) => cmd === 'python3')).toBe(false);
    // Routed to setupErrors → setupFailed, NOT to the code-fix/failures path.
    expect(result.setupFailed).toBe(true);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(false);
  });

  it('Go runner + go.mod and a failing `go mod download` → setupErrors → setupFailed', async () => {
    vi.mocked(fs.existsSync).mockImplementation(
      (p: unknown) => typeof p === 'string' && p.endsWith('/go.mod'),
    );
    execFileAsync.mockImplementation((cmd: string, args: string[]) =>
      cmd === 'go' && Array.isArray(args) && args[0] === 'mod'
        ? Promise.reject(new Error('go: example.com/foo@v1.2.3: reading module: connection refused'))
        : Promise.resolve('ok'),
    );

    const goRunner: TestRunner = { cmd: 'go', args: ['test', './...'], cwd: '/tmp/workspace', label: 'go test (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [goRunner] },
    );

    const dlCall = execFileAsync.mock.calls.find(
      ([cmd, args]) => cmd === 'go' && Array.isArray(args) && args[0] === 'mod',
    );
    expect(dlCall?.[1]).toEqual(['mod', 'download']);
    // `go test` never ran — the runner was skipped after the download failure.
    expect(execFileAsync.mock.calls.some(
      ([cmd, args]) => cmd === 'go' && Array.isArray(args) && args[0] === 'test',
    )).toBe(false);
    expect(result.setupFailed).toBe(true);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(false);
  });

  it('Rust runner + Cargo.toml and a failing `cargo fetch` → setupErrors → setupFailed', async () => {
    vi.mocked(fs.existsSync).mockImplementation(
      (p: unknown) => typeof p === 'string' && p.endsWith('/Cargo.toml'),
    );
    execFileAsync.mockImplementation((cmd: string, args: string[]) =>
      cmd === 'cargo' && Array.isArray(args) && args[0] === 'fetch'
        ? Promise.reject(new Error('error: failed to fetch `https://github.com/rust-lang/crates.io-index`'))
        : Promise.resolve('ok'),
    );

    const cargoRunner: TestRunner = { cmd: 'cargo', args: ['test'], cwd: '/tmp/workspace', label: 'cargo test (root)' };
    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [cargoRunner] },
    );

    const fetchCall = execFileAsync.mock.calls.find(
      ([cmd, args]) => cmd === 'cargo' && Array.isArray(args) && args[0] === 'fetch',
    );
    expect(fetchCall?.[1]).toEqual(['fetch', '--manifest-path', '/tmp/workspace/Cargo.toml']);
    // `cargo test` never ran — skipped after the fetch failure.
    expect(execFileAsync.mock.calls.some(
      ([cmd, args]) => cmd === 'cargo' && Array.isArray(args) && args[0] === 'test',
    )).toBe(false);
    expect(result.setupFailed).toBe(true);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(false);
  });
});

describe('testFixLoop — in-loop environment-failure routing (maxFixAttempts: 0)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockImplementation(defaultExistsSync);
  });

  it('runner exits non-zero with a ModuleNotFoundError → setupErrors → setupFailed (not a FAILED/code-fix verdict)', async () => {
    execFileAsync.mockRejectedValue(
      new Error("Command failed: python3 -m pytest -x -q\nModuleNotFoundError: No module named 'pytest'"),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [REAL_RUNNER] },
    );

    // Env signature → skipped to setupErrors, surfaced as setupFailed. The
    // code-fix path (testFailuresDetected) must NOT fire.
    expect(result.setupFailed).toBe(true);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(false);
  });

  it('runner exits non-zero with a missing shared library (.so) → setupErrors → setupFailed', async () => {
    execFileAsync.mockRejectedValue(
      new Error('Command failed: npm test\nerror while loading shared libraries: libatk-1.0.so.0: cannot open shared object file: No such file or directory'),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [NPM_RUNNER] },
    );

    expect(result.setupFailed).toBe(true);
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(false);
  });

  it('a real test failure that QUOTES an env phrase stays a code failure (precedence preserved, no fall-open)', async () => {
    execFileAsync.mockRejectedValue(
      new Error("Command failed: python3 -m pytest -x -q\nFAILED test_imports.py::test_optional_dep - AssertionError: expected 'No module named foo' in message\n1 failed in 0.12s"),
    );

    const result = await testFixLoop(
      null, statusRef, undefined, undefined, undefined,
      undefined, undefined, undefined,
      { maxFixAttempts: 0, runnersOverride: [REAL_RUNNER] },
    );

    // Real-failure evidence (FAILED line + "1 failed") guards against the env
    // misroute — this stays a genuine test failure, not a setup fall-open.
    expect(result.setupFailed).toBeFalsy();
    expect(result.passed).toBe(false);
    expect(result.testFailuresDetected).toBe(true);
  });
});
