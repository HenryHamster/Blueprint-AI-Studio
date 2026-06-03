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
    existsSync: vi.fn(() => true), // node_modules present → skip npm ci
    readFileSync: vi.fn(() => ''),
    unlinkSync: vi.fn(),
  },
}));

import { testFixLoop, matchesSizeBreachSignature } from './test-fix-loop.js';
import { runClaudeCode } from '../providers/runners.js';
import type { TestRunner } from './test-runners.js';

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
