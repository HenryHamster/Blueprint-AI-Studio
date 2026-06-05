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
    // execFileAsync drives the acceptance probe; mocked so the fall-open tests
    // never shell out. Default resolves (probe pass) but is irrelevant unless a
    // probe is detected (detectAcceptanceProbes defaults to [] below).
    execFileAsync: vi.fn().mockResolvedValue(''),
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
  // detectAcceptanceProbes is mocked so the fall-open paths never scan the real
  // filesystem; classifyProbeOutcome stays REAL so the probe verdict reflects the
  // actual signature classification of the mocked execFileAsync error.
  return {
    ...actual,
    detectTestRunners: vi.fn(),
    detectAutoTestRunners: vi.fn(),
    detectAcceptanceProbes: vi.fn(),
  };
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
import { persistAndPostStatus, execFileAsync } from '../index.js';
import { detectTestRunners, detectAutoTestRunners, detectAcceptanceProbes } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

const PY_PROBE = {
  cmd: 'python3', args: ['-m', 'compileall', '-q', '/tmp/workspace'],
  cwd: '/tmp/workspace', ecosystem: 'python' as const, label: 'python3 -m compileall (root)',
};
// An operator-declared build command probe (build_cmd).
const DECLARED_PROBE = {
  cmd: 'npm', args: ['run', 'build'],
  cwd: '/tmp/workspace/admin-ui', ecosystem: 'node' as const,
  label: 'npm run build (admin-ui)', declared: true,
};

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

type Probe = {
  ran: boolean; ecosystem: string; outcome: string;
  passed: boolean; could_not_run: boolean; output: string; label: string;
  declared?: boolean;
};
function parsedVerdict(): {
  detected: boolean; passed: boolean; tests: unknown[]; output: string;
  recovered?: boolean; recovered_directories?: Array<{ path: string; type: string; test_cmd: string }>;
  probe?: Probe;
} {
  return JSON.parse(lastPostedPayload().logs as string) as {
    detected: boolean; passed: boolean; tests: unknown[]; output: string;
    recovered?: boolean; recovered_directories?: Array<{ path: string; type: string; test_cmd: string }>;
    probe?: Probe;
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
    // Default: no probeable ecosystem, so the existing fall-open tests carry no
    // probe field and behave exactly as before. Probe tests override this.
    vi.mocked(detectAcceptanceProbes).mockReturnValue([]);
    vi.mocked(execFileAsync).mockResolvedValue('');
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

  it('persistent PARTIAL setup failure → FAILED (fails closed, distinguishable via partialSetupFailed)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    // A sibling runner ran but another failed setup — the partial-setup signal
    // persists across the bounded retries. Must fail the gate closed (FAILED
    // MERGE_TEST_RESULT), NOT report a clean detected=true/passed=true verdict.
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: false,
      partialSetupFailed: true,
      failureSummary: 'Partial setup failure — [pytest (backend)] npm ci failed: registry timeout',
    });

    await handleVerifyTests('t5p', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict() as {
      detected: boolean; passed: boolean; setupFailed?: boolean; partialSetupFailed?: boolean; output: string;
    };
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
    expect(verdict.partialSetupFailed).toBe(true);
    // An all-runners setupFailed escalation is NOT the same condition.
    expect(verdict.setupFailed).toBeFalsy();
    expect(verdict.output).toContain('registry timeout');
    // Bounded retry fired: testFixLoop was re-invoked past the first attempt.
    expect(vi.mocked(testFixLoop).mock.calls.length).toBeGreaterThan(1);
  });

  it('environment-style setup failure output also posts FAILED with setupFailed=true', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: false,
      setupFailed: true,
      failureSummary: "Traceback (most recent call last):\nModuleNotFoundError: No module named 'sqlalchemy'",
    });

    await handleVerifyTests('t4b', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    const verdict = parsedVerdict() as { detected: boolean; setupFailed?: boolean; output: string };
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBe(true);
    expect(verdict.output).toContain("No module named 'sqlalchemy'");
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

  it('real failure whose output quotes a placeholder phrase → detected=true, passed=false, no stale-command recovery', async () => {
    // The suite genuinely ran and failed; its failure text merely QUOTES a
    // placeholder literal ("no test specified") as assertion data. Because the
    // tightened classifier keeps this a real run, testFixLoop returns
    // noTestsExecuted unset — so verify-tests must NOT enter the noTestsExecuted
    // branch (no stale-command recovery / detected=false fall-open) and must
    // report the real failure verdict instead.
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: true,
      failureSummary: "FAILED test_cli.py::test_help - AssertionError: expected 'no test specified' in output",
    });

    await handleVerifyTests('t9b', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.output).toContain('no test specified');
    // No stale-command recovery: testFixLoop invoked exactly once (the stored
    // runner), never re-run with a replacement override.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
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

  // ── Acceptance probe on the detected=false fall-open paths ────────────────
  it('no suite + probe PASSES → detected=false fall-open carries probe.passed=true', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);
    vi.mocked(execFileAsync).mockResolvedValue(''); // compileall exits 0

    await handleVerifyTests('p1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe).toBeDefined();
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.passed).toBe(true);
    expect(verdict.probe?.could_not_run).toBe(false);
    expect(verdict.probe?.ecosystem).toBe('python');
    expect(verdict.output).toContain('Acceptance probe passed');
  });

  it('no suite + probe FAILS (syntax error) → detected=false but probe.passed=false (backend blocks)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);
    // execFileAsync rejects with a clean compile error → classifyProbeOutcome=fail.
    const err = Object.assign(new Error('Command failed\n  File "x.py", line 2\n    def\n       ^\nSyntaxError: invalid syntax'), {
      stdout: '', stderr: 'SyntaxError: invalid syntax', status: 1, code: undefined,
    });
    vi.mocked(execFileAsync).mockRejectedValue(err);

    await handleVerifyTests('p2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.passed).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(false);
    expect(verdict.probe?.outcome).toBe('fail');
    expect(verdict.probe?.output).toContain('SyntaxError');
    expect(verdict.output).toContain('Acceptance probe FAILED');
  });

  it('no suite + probe CANNOT RUN (missing deps) → detected=false fall-open, probe.could_not_run=true (no block)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);
    const err = Object.assign(new Error('spawn python3 ENOENT'), { code: 'ENOENT' });
    vi.mocked(execFileAsync).mockRejectedValue(err);

    await handleVerifyTests('p3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.passed).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(true);
    expect(verdict.probe?.outcome).toBe('could_not_run');
    expect(verdict.output).toContain('Acceptance probe could not run');
  });

  it('all-skipped suite + probe FAILS → detected=false, all_skipped reason preserved, probe.passed=false', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: true, testFailuresDetected: false, noTestsExecuted: true, noTestsReason: 'all_skipped',
    });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);
    const err = Object.assign(new Error('SyntaxError: invalid syntax'), { stderr: 'SyntaxError: invalid syntax', status: 1 });
    vi.mocked(execFileAsync).mockRejectedValue(err);

    await handleVerifyTests('p4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict() as { detected: boolean; noTestsReason?: string; probe?: Probe };
    expect(verdict.detected).toBe(false);
    expect(verdict.noTestsReason).toBe('all_skipped');
    expect(verdict.probe?.passed).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(false);
  });

  // ── Declared build_cmd probe on the fall-open paths ───────────────────────
  it('no suite + declared build PASSES → fall-open carries declared probe + wording', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    vi.mocked(execFileAsync).mockResolvedValue(''); // npm run build exits 0

    await handleVerifyTests('d1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.passed).toBe(true);
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)`');
  });

  it('no suite + declared build FAILS (clean non-zero) → probe.passed=false (backend blocks)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // A clean build error with no missing-tool/dep signature → fail for declared.
    const err = Object.assign(new Error('Command failed: npm run build\nwebpack: build error'), {
      stdout: '', stderr: 'webpack: build error in module foo', status: 1, code: undefined,
    });
    vi.mocked(execFileAsync).mockRejectedValue(err);

    await handleVerifyTests('d2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.passed).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(false);
    expect(verdict.probe?.outcome).toBe('fail');
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)` and it FAILED');
  });

  it('no suite + declared build CANNOT RUN (missing tool) → could_not_run, no block', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    const err = Object.assign(new Error('failed'), {
      stdout: '', stderr: 'bash: npm: command not found', status: 127, code: undefined,
    });
    vi.mocked(execFileAsync).mockRejectedValue(err);

    await handleVerifyTests('d3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(true);
    expect(verdict.probe?.outcome).toBe('could_not_run');
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.output).toContain('could not run');
  });

  it('detected real pass → no probe run (probe only runs on fall-open)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);

    await handleVerifyTests('p5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.probe).toBeUndefined();
    // The probe is never even detected on a real-suite verdict.
    expect(vi.mocked(detectAcceptanceProbes)).not.toHaveBeenCalled();
    expect(vi.mocked(execFileAsync)).not.toHaveBeenCalled();
  });

  it('runs testFixLoop once with maxFixAttempts 0 (no fix loop)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
    const opts = vi.mocked(testFixLoop).mock.calls[0].at(-1) as { maxFixAttempts: number };
    expect(opts.maxFixAttempts).toBe(0);
  });

  it('declared build_cmd probe runs under larger timeout than generic probes', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    // Return both a generic probe and a declared probe to verify different timeouts
    const genericProbe = { ...PY_PROBE };
    const declaredProbe = { ...DECLARED_PROBE };
    vi.mocked(detectAcceptanceProbes).mockReturnValue([genericProbe, declaredProbe]);

    // Track calls to execFileAsync to verify timeout values
    const callsWithTimeouts: Array<{ timeoutMs?: number }> = [];
    vi.mocked(execFileAsync).mockImplementation((_cmd: unknown, _args: unknown, opts?: unknown) => {
      callsWithTimeouts.push(opts as { timeoutMs?: number });
      return Promise.resolve('');
    });

    await handleVerifyTests('timeout1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Should have 2 calls to execFileAsync (one generic, one declared)
    expect(callsWithTimeouts).toHaveLength(2);
    // The generic Python probe should use the smaller timeout (TEST_BASELINE_TIMEOUT = 60_000)
    expect(callsWithTimeouts[0].timeoutMs).toBe(60_000);
    // The declared build probe should use the larger timeout (TEST_RUNNER_TIMEOUT = 180_000)
    expect(callsWithTimeouts[1].timeoutMs).toBe(180_000);
  });
});
