import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock ../index.js so module-level imports resolve and main() never runs.
// Keep strVal/sanitize/resolveBaseBranch real (pure helpers the handler relies
// on) and stub the IO surface.
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    validateBranchName: vi.fn((b: string) => b),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue(undefined),
}));

vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  // detectAutoTestRunners is mocked so the stale-command recovery path is
  // deterministic (no real filesystem scan). describeRunnerDirectory stays real
  // so recovered_directories reflects the actual cmd→type/path mapping.
  return { ...actual, detectTestRunners: vi.fn(), detectAutoTestRunners: vi.fn() };
});

vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return { ...actual, testFixLoop: vi.fn() };
});

vi.mock('fs', () => ({
  default: {
    writeFileSync: vi.fn(),
  },
}));

import { handleVerifyTests } from './verify-tests.js';
import { persistAndPostStatus } from '../index.js';
import { detectTestRunners, detectAutoTestRunners } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

const RUNNER = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' };
// A stored runner pointing at a now-stale command (e.g. a dropped Makefile target).
const STALE_RUNNER = { cmd: 'make', args: ['test'], cwd: '/tmp/workspace', label: 'make test (Makefile)' };
// A real replacement suite auto-detected at a different path/command.
const REPLACEMENT_RUNNER = {
  cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'],
  cwd: '/tmp/workspace/services/api', label: 'pytest (services/api)',
};

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

function parsedVerdict(): {
  detected: boolean; passed: boolean; tests: unknown[]; output: string;
  recovered?: boolean; recovered_directories?: Array<{ path: string; type: string; test_cmd: string }>;
} {
  return JSON.parse(lastPostedPayload().logs as string) as {
    detected: boolean; passed: boolean; tests: unknown[]; output: string;
    recovered?: boolean; recovered_directories?: Array<{ path: string; type: string; test_cmd: string }>;
  };
}

describe('handleVerifyTests', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
    // Default: auto-detection finds nothing to recover with, so the existing
    // placeholder/empty fall-open tests behave exactly as before. Recovery
    // tests override this per-case.
    vi.mocked(detectAutoTestRunners).mockReturnValue([]);
  });

  it('passing suite → COMPLETED with detected=true, passed=true', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const post = lastPostedPayload();
    expect(post.status).toBe('COMPLETED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.tests).toHaveLength(1);
  });

  it('failing suite → COMPLETED with detected=true, passed=false and output summary', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: true,
      failureSummary: 'AssertionError: expected 1 to equal 2',
    });

    await handleVerifyTests('t2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.output).toContain('AssertionError');
    expect((verdict.tests[0] as { status: string }).status).toBe('fail');
  });

  it('no runner detected → COMPLETED with detected=false (backend falls back)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);

    await handleVerifyTests('t3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(vi.mocked(testFixLoop)).not.toHaveBeenCalled();
    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
    expect(verdict.tests).toHaveLength(0);
  });

  it('persistent setup failure → FAILED (routes to merge-gate retry, not an instant UNVERIFIED downgrade)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    // Setup keeps failing across the bounded in-handler retries.
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: false,
      setupFailed: true,
      failureSummary: 'npm ci failed: registry timeout',
    });

    await handleVerifyTests('t4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict() as { detected: boolean; setupFailed?: boolean; output: string };
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBe(true);
    expect(verdict.output).toContain('registry timeout');
  });

  it('placeholder suite (failed but no real tests) → detected=false (proceed-without-checks fallback)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: true,
      noTestsExecuted: true,
      failureSummary: 'Error: no test specified',
    });

    await handleVerifyTests('t6', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
    expect(verdict.tests).toHaveLength(0);
  });

  it('empty-but-passing suite (no real tests) → detected=false, not a vacuous pass', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: true,
      testFailuresDetected: false,
      noTestsExecuted: true,
    });

    await handleVerifyTests('t7', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
  });

  it('real one-test pass (noTestsExecuted unset) → detected=true, passed=true', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t8', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
  });

  it('real test failure (noTestsExecuted unset) → still detected=true, passed=false (verification not weakened)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: true,
      failureSummary: 'AssertionError: expected 1 to equal 2',
    });

    await handleVerifyTests('t9', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.output).toContain('AssertionError');
  });

  it('stale stored command + real replacement that PASSES → recovered=true, detected/passed reflect replacement', async () => {
    // Stored runner is the stale `make test`; auto-detection surfaces a real
    // pytest suite at services/api.
    vi.mocked(detectTestRunners).mockReturnValue([STALE_RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([REPLACEMENT_RUNNER]);
    vi.mocked(testFixLoop)
      // stored run: stale command, no real tests executed
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, noTestsExecuted: true, failureSummary: 'No rule to make target `test\'' })
      // replacement run: real suite, passes
      .mockResolvedValueOnce({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t10', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.recovered).toBe(true);
    expect(verdict.recovered_directories).toEqual([
      { path: 'services/api', type: 'python', test_cmd: 'python3 -m pytest -x -q' },
    ]);
    expect((verdict.tests[0] as { name: string }).name).toBe('pytest (services/api)');
    // testFixLoop called twice: stored runner, then the runnersOverride replacement.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(2);
    const recoveryOpts = vi.mocked(testFixLoop).mock.calls[1].at(-1) as { runnersOverride?: unknown[]; maxFixAttempts: number };
    expect(recoveryOpts.maxFixAttempts).toBe(0);
    expect(recoveryOpts.runnersOverride).toEqual([REPLACEMENT_RUNNER]);
  });

  it('stale stored command + real replacement that FAILS → recovered=true, passed=false, failure output carried', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([STALE_RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([REPLACEMENT_RUNNER]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, noTestsExecuted: true, failureSummary: 'no test specified' })
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, failureSummary: 'AssertionError: expected 1 to equal 2' });

    await handleVerifyTests('t11', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.recovered).toBe(true);
    expect(verdict.output).toContain('AssertionError');
    expect((verdict.tests[0] as { status: string }).status).toBe('fail');
  });

  it('stale stored command + replacement also runs no real tests → no recovery, detected=false fall-open', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([STALE_RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([REPLACEMENT_RUNNER]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, noTestsExecuted: true, failureSummary: 'no test specified' })
      // replacement is ALSO just another placeholder/empty suite
      .mockResolvedValueOnce({ passed: true, testFailuresDetected: false, noTestsExecuted: true });

    await handleVerifyTests('t12', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
    expect(verdict.recovered).toBeUndefined();
    expect(verdict.recovered_directories).toBeUndefined();
  });

  it('stale stored command + replacement setup fails → FAILED with setupFailed so the merge gate retries', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([STALE_RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([REPLACEMENT_RUNNER]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, noTestsExecuted: true, failureSummary: 'no test specified' })
      .mockResolvedValue({ passed: false, testFailuresDetected: false, setupFailed: true, failureSummary: 'npm ci failed' });

    await handleVerifyTests('t13', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    const verdict = parsedVerdict() as { detected: boolean; setupFailed?: boolean; output: string };
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBe(true);
    expect(verdict.output).toContain('npm ci failed');
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(4);
  });

  it('stale stored command + replacement identical to stored runner → filtered out, no recovery', async () => {
    // Auto-detection re-surfaces the same dead runner (e.g. there was no stored
    // config and the placeholder IS the auto-detected one). Must not re-invoke it.
    vi.mocked(detectTestRunners).mockReturnValue([STALE_RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([{ ...STALE_RUNNER }]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({ passed: false, testFailuresDetected: true, noTestsExecuted: true, failureSummary: 'no test specified' });

    await handleVerifyTests('t14', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.recovered).toBeUndefined();
    // testFixLoop ran ONCE (stored only) — the replacement was filtered out, so
    // no second run.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
  });

  it('real pass (noTestsExecuted unset) → recovery NOT invoked (happy path not regressed)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(detectAutoTestRunners).mockReturnValue([REPLACEMENT_RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t15', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.recovered).toBeUndefined();
    // No recovery path: testFixLoop called exactly once, detectAutoTestRunners never consulted.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(detectAutoTestRunners)).not.toHaveBeenCalled();
  });

  it('runs testFixLoop once with maxFixAttempts 0 (no fix loop)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
    const opts = vi.mocked(testFixLoop).mock.calls[0].at(-1) as { maxFixAttempts: number };
    expect(opts.maxFixAttempts).toBe(0);
  });
});
