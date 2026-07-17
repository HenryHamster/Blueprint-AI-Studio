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
  // discoverDocumentedCommands is mocked so the documented-command probe tests
  // can drive a specific discovered command (build-intent vs lint) without a real
  // filesystem/diff scan. runnerFromCommand stays REAL so the mocked command
  // string is tokenized into the actual {cmd,args} the probe executes.
  return {
    ...actual,
    detectTestRunners: vi.fn(),
    detectAutoTestRunners: vi.fn(),
    detectAcceptanceProbes: vi.fn(),
    discoverDocumentedCommands: vi.fn(),
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
import { detectTestRunners, detectAutoTestRunners, detectAcceptanceProbes, discoverDocumentedCommands } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

// Must match process.env.AGENT_WORKSPACE so describeRunnerDirectory (which reads
// that env var at module load time in test-runners.ts) strips the right prefix.
const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

const PY_PROBE = {
  cmd: 'python3', args: ['-m', 'compileall', '-q', WORKSPACE],
  cwd: WORKSPACE, ecosystem: 'python' as const, label: 'python3 -m compileall (root)',
};
// An operator-declared build command probe (build_cmd).
const DECLARED_PROBE = {
  cmd: 'npm', args: ['run', 'build'],
  cwd: `${WORKSPACE}/admin-ui`, ecosystem: 'node' as const,
  label: 'npm run build (admin-ui)', declared: true,
};

const RUNNER = { cmd: 'npm', args: ['test'], cwd: WORKSPACE, label: 'npm test (root)' };
// A stored runner pointing at a now-stale command (e.g. a dropped Makefile target).
const STALE_RUNNER = { cmd: 'make', args: ['test'], cwd: WORKSPACE, label: 'make test (Makefile)' };
// A real replacement suite auto-detected at a different path/command.
const REPLACEMENT_RUNNER = {
  cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'],
  cwd: `${WORKSPACE}/services/api`, label: 'pytest (services/api)',
};

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

type Probe = {
  ran: boolean; ecosystem: string; outcome: string;
  passed: boolean; could_not_run: boolean; output: string; label: string;
  declared?: boolean; wrote_no_artifact?: boolean;
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
    // Default: the PR introduces/documents no runnable command, so the
    // documented-command probe is a no-op and existing tests are unaffected.
    // The no-op-build tests override this per-case.
    vi.mocked(discoverDocumentedCommands).mockReturnValue([]);
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
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown) =>
      cmd === 'git' ? Promise.resolve('') : Promise.reject(err),
    );

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

  // Distinguish the git working-tree snapshot calls from the build command so a
  // test can simulate a real build (tree changes) vs a no-op build (tree static).
  function mockProbeWithTree(before: string, after: string): void {
    let gitStatusCalls = 0;
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
      if (cmd === 'git') {
        const a = args as string[];
        if (a[0] === 'status') {
          // snapshotWorkingTree calls: 1st → before, 2nd → after
          gitStatusCalls += 1;
          return Promise.resolve(gitStatusCalls === 1 ? before : after);
        }
        // git diff calls (from computePrDiff) return empty
        return Promise.resolve('');
      }
      return Promise.resolve(''); // the declared build exits 0
    });
  }

  it('no suite + declared build PASSES but wrote NOTHING → wrote_no_artifact flag + no-op wording', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // before === after: the build wrote no new output (no-op build).
    mockProbeWithTree('!! node_modules/\n', '!! node_modules/\n');

    await handleVerifyTests('noop1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.probe?.passed).toBe(true);
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.probe?.wrote_no_artifact).toBe(true);
    expect(verdict.output).toContain('wrote no artifact');
  });

  it('no suite + declared build PASSES and wrote output → wrote_no_artifact false, normal wording', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // after has a NEW untracked artifact relative to before → real build.
    mockProbeWithTree('!! node_modules/\n', '!! node_modules/\n?? dist/bundle.js\n');

    await handleVerifyTests('real1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.probe?.passed).toBe(true);
    expect(verdict.probe?.wrote_no_artifact).toBe(false);
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)` and it passed');
  });

  it('no suite + declared build PASSES but tree unobservable (git fails) → flag unset', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // git snapshot rejects (not a repo / git unavailable); build still exits 0.
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown) => {
      if (cmd === 'git') return Promise.reject(new Error('not a git repository'));
      return Promise.resolve('');
    });

    await handleVerifyTests('unobs1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.probe?.passed).toBe(true);
    // Observation unavailable → flag must be UNSET so the backend keeps probe_passed.
    expect(verdict.probe?.wrote_no_artifact).toBeUndefined();
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)` and it passed');
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

  it('detected real pass with only a GENERIC probe → no probe attaches (declared-only on passing path)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    // A generic (non-declared) probe is available, but the passing path runs
    // DECLARED builds only — the generic probe is filtered out, so no probe runs.
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PY_PROBE]);

    await handleVerifyTests('p5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.probe).toBeUndefined();
    // detectAcceptanceProbes IS consulted now (to filter for a declared build),
    // but the generic PY_PROBE is dropped, so no acceptance-probe command runs.
    const nonGitCalls = vi.mocked(execFileAsync).mock.calls.filter(c => c[0] !== 'git');
    expect(nonGitCalls).toHaveLength(0);
  });

  // ── Declared build_cmd probe on the PASSING-suite path ────────────────────
  it('detected+passing suite + declared build FAILS → declared probe passed=false (backend blocks)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // git snapshots resolve; the declared build itself exits non-zero cleanly.
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown) => {
      if (cmd === 'git') return Promise.resolve('');
      return Promise.reject(Object.assign(new Error('Command failed: npm run build\nwebpack: build error'), {
        stdout: '', stderr: 'webpack: build error in module foo', status: 1, code: undefined,
      }));
    });

    await handleVerifyTests('dp1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    // The suite still passed — the agent does NOT flip detected/passed; it only
    // attaches the declared probe and lets the backend decide the block.
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.probe?.ran).toBe(true);
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.probe?.passed).toBe(false);
    expect(verdict.probe?.could_not_run).toBe(false);
    expect(verdict.probe?.outcome).toBe('fail');
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)` and it FAILED');
  });

  it('detected+passing suite + declared build PASSES → declared probe attached, verdict still pass', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    // before !== after: the build wrote a real artifact → normal pass wording.
    mockProbeWithTree('!! node_modules/\n', '!! node_modules/\n?? dist/bundle.js\n');

    await handleVerifyTests('dp2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.probe?.passed).toBe(true);
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.probe?.wrote_no_artifact).toBe(false);
    expect(verdict.output).toContain('declared build command `npm run build (admin-ui)` and it passed');
  });

  it('detected+passing suite + declared build CANNOT RUN → pass preserved, no probe block', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown) => {
      if (cmd === 'git') return Promise.resolve('');
      return Promise.reject(Object.assign(new Error('failed'), {
        stdout: '', stderr: 'bash: npm: command not found', status: 127, code: undefined,
      }));
    });

    await handleVerifyTests('dp3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.probe?.could_not_run).toBe(true);
    expect(verdict.probe?.outcome).toBe('could_not_run');
    expect(verdict.probe?.declared).toBe(true);
    expect(verdict.output).toContain('could not run');
  });

  it('detected+passing suite + NO declared build_cmd → no probe (passing path unchanged)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    // No probeable ecosystem at all → declared-only filter is empty → no probe.
    vi.mocked(detectAcceptanceProbes).mockReturnValue([]);

    await handleVerifyTests('dp4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.probe).toBeUndefined();
  });

  it('detected FAILING suite → no declared build probe (only runs on the passing path)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false, testFailuresDetected: true, failureSummary: 'AssertionError',
    });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([DECLARED_PROBE]);

    await handleVerifyTests('dp5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.probe).toBeUndefined();
    // The declared build probe must NOT run on the failing path.
    const nonGitCalls = vi.mocked(execFileAsync).mock.calls.filter(c => c[0] !== 'git');
    expect(nonGitCalls).toHaveLength(0);
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
    const probeCalls: Array<{ cmd: string; timeoutMs?: number }> = [];
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, _args: unknown, opts?: unknown) => {
      probeCalls.push({ cmd: cmd as string, timeoutMs: (opts as { timeoutMs?: number } | undefined)?.timeoutMs });
      return Promise.resolve('');
    });

    await handleVerifyTests('timeout1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Filter out the documented-command probe's git-diff calls — this test is
    // about the two acceptance-probe timeouts (one generic, one declared).
    const callsWithTimeouts = probeCalls.filter(c => c.cmd !== 'git');
    expect(callsWithTimeouts).toHaveLength(2);
    // The generic Python probe should use the smaller timeout (TEST_BASELINE_TIMEOUT = 60_000)
    expect(callsWithTimeouts[0].timeoutMs).toBe(60_000);
    // The declared build probe should use the larger timeout (TEST_RUNNER_TIMEOUT = 180_000)
    expect(callsWithTimeouts[1].timeoutMs).toBe(180_000);
  });

  // ── Documented/PR-introduced build command no-op detection ──────────────────
  describe('documentedCommands wrote_no_artifact (no-op build) detection', () => {
    type DocCmds = {
      ran: boolean; outcome: string; passed: boolean; could_not_run: boolean;
      output: string; label: string; wrote_no_artifact?: boolean;
    };
    function docVerdict(): { documentedCommands?: DocCmds; output: string } {
      return JSON.parse(lastPostedPayload().logs as string) as { documentedCommands?: DocCmds; output: string };
    }
    // A build-intent (heavy) documented command and a non-build (lint) one.
    const DOC_BUILD = {
      command: 'npm run build', cwd: '/tmp/workspace',
      source: 'package.json scripts.build', label: 'npm run build (package.json)', heavy: true,
    };
    const DOC_LINT = {
      command: 'npm run lint', cwd: '/tmp/workspace',
      source: 'package.json scripts.lint', label: 'npm run lint (package.json)', heavy: false,
    };
    // Drive the PR-diff git passes (return one changed file so the probe runs)
    // and the before/after `git status` snapshots around the documented build.
    function mockDocBuildTree(before: string, after: string): void {
      let statusCalls = 0;
      vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
        if (cmd === 'git') {
          const a = args as string[];
          if (a.includes('--name-only')) return Promise.resolve('package.json');
          if (a.includes('status')) {
            statusCalls += 1;
            return Promise.resolve(statusCalls === 1 ? before : after);
          }
          return Promise.resolve(''); // the --unified=0 diff
        }
        return Promise.resolve(''); // the documented build exits 0
      });
    }
    function sawGitStatus(): boolean {
      return vi.mocked(execFileAsync).mock.calls.some(
        c => c[0] === 'git' && Array.isArray(c[1]) && c[1].includes('status'),
      );
    }

    beforeEach(() => {
      vi.mocked(detectTestRunners).mockReturnValue([]); // no unit suite
    });

    it('no-op documented build (passes, wrote NOTHING) → wrote_no_artifact true + no-op wording', async () => {
      vi.mocked(discoverDocumentedCommands).mockReturnValue([DOC_BUILD]);
      mockDocBuildTree('!! node_modules/\n', '!! node_modules/\n'); // before === after

      await handleVerifyTests('doc-noop', payload, task, 'tok', 'o', 'r', null, { value: '' });

      const verdict = docVerdict();
      expect(verdict.documentedCommands?.passed).toBe(true);
      expect(verdict.documentedCommands?.wrote_no_artifact).toBe(true);
      expect(verdict.output).toContain('wrote no artifact');
    });

    it('real documented build (passes, wrote output) → wrote_no_artifact false, normal wording', async () => {
      vi.mocked(discoverDocumentedCommands).mockReturnValue([DOC_BUILD]);
      mockDocBuildTree('!! node_modules/\n', '!! node_modules/\n?? dist/bundle.js\n');

      await handleVerifyTests('doc-real', payload, task, 'tok', 'o', 'r', null, { value: '' });

      const verdict = docVerdict();
      expect(verdict.documentedCommands?.passed).toBe(true);
      expect(verdict.documentedCommands?.wrote_no_artifact).toBe(false);
      expect(verdict.output).toContain('command probe passed');
      expect(verdict.output).not.toContain('wrote no artifact');
    });

    it('documented LINT command (not build-intent) → never snapshotted, never flagged', async () => {
      vi.mocked(discoverDocumentedCommands).mockReturnValue([DOC_LINT]);
      mockDocBuildTree('!! node_modules/\n', '!! node_modules/\n'); // tree static, but irrelevant

      await handleVerifyTests('doc-lint', payload, task, 'tok', 'o', 'r', null, { value: '' });

      const verdict = docVerdict();
      expect(verdict.documentedCommands?.passed).toBe(true);
      // A clean lint correctly produces no artifact — it must NOT be flagged and
      // the tree must never have been snapshotted for it.
      expect(verdict.documentedCommands?.wrote_no_artifact).toBeUndefined();
      expect(sawGitStatus()).toBe(false);
      expect(verdict.output).toContain('command probe passed');
    });

    it('no-op build but tree unobservable (git status fails) → flag unset', async () => {
      vi.mocked(discoverDocumentedCommands).mockReturnValue([DOC_BUILD]);
      vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
        if (cmd === 'git') {
          const a = args as string[];
          if (a.includes('--name-only')) return Promise.resolve('package.json');
          if (a.includes('status')) return Promise.reject(new Error('not a git repository'));
          return Promise.resolve('');
        }
        return Promise.resolve(''); // build exits 0
      });

      await handleVerifyTests('doc-unobs', payload, task, 'tok', 'o', 'r', null, { value: '' });

      const verdict = docVerdict();
      expect(verdict.documentedCommands?.passed).toBe(true);
      // Observation unavailable → flag UNSET so the backend keeps documented_command_passed.
      expect(verdict.documentedCommands?.wrote_no_artifact).toBeUndefined();
      expect(verdict.output).toContain('command probe passed');
    });
  });

  // ── Newly-disabled-tests visibility probe (diff-aware, never blocks) ────────
  describe('disabledTests visibility field', () => {
    type VerdictWithDisabled = {
      detected: boolean; passed: boolean;
      disabledTests?: { count: number; markers: Array<{ file: string; marker: string; line: string }> };
    };
    function verdictWithDisabled(): VerdictWithDisabled {
      return JSON.parse(lastPostedPayload().logs as string) as VerdictWithDisabled;
    }
    // Drive the shared PR-diff `git diff` passes (--name-only then --unified=0)
    // off a per-file added-lines map; every non-git call resolves empty.
    function mockGitDiff(addedByFile: Record<string, string[]>): void {
      const files = Object.keys(addedByFile);
      const unified = files
        .map(f => `diff --git a/${f} b/${f}\n--- a/${f}\n+++ b/${f}\n@@ -0,0 +1 @@\n${addedByFile[f].map(l => `+${l}`).join('\n')}`)
        .join('\n');
      vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
        if (cmd === 'git') {
          const a = args as string[];
          if (a.includes('--name-only')) return Promise.resolve(files.join('\n'));
          if (a.includes('--unified=0')) return Promise.resolve(unified);
        }
        return Promise.resolve('');
      });
    }

    it('surfaces disabledTests with the right count when the PR newly skips tests', async () => {
      vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
      vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
      mockGitDiff({ 'src/foo.test.ts': ['it.skip("flaky", () => {})', 'describe.skip("grp", () => {})'] });

      await handleVerifyTests('dt1', payload, task, 'tok', 'o', 'r', null, { value: '' });

      const verdict = verdictWithDisabled();
      // Visibility-only: the suite verdict is unchanged (still a clean pass).
      expect(verdict.detected).toBe(true);
      expect(verdict.passed).toBe(true);
      const dt = verdict.disabledTests;
      expect(dt).toBeDefined();
      expect(dt?.count).toBe(2);
      expect(dt?.markers.map(m => m.marker).sort()).toEqual(['describe.skip', 'it.skip']);
      expect(dt?.markers[0].file).toBe('src/foo.test.ts');
    });

    it('omits disabledTests entirely when the PR introduces no new skips', async () => {
      vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
      vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
      mockGitDiff({ 'src/foo.test.ts': ['expect(value).toBe(1);'] });

      await handleVerifyTests('dt2', payload, task, 'tok', 'o', 'r', null, { value: '' });

      expect(verdictWithDisabled().disabledTests).toBeUndefined();
    });

    it('fails setup safely when the PR diff cannot be computed', async () => {
      vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
      vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
      // A resolved base ref whose actual diff still fails is not authoritative
      // provenance. It must not silently expand to an unscoped suite.
      vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
        if (cmd === 'git' && (args as string[]).includes('--name-only')) {
          return Promise.reject(new Error('unknown revision origin/main'));
        }
        return Promise.resolve('');
      });

      await handleVerifyTests('dt3', payload, task, 'tok', 'o', 'r', null, { value: '' });

      expect(vi.mocked(testFixLoop)).not.toHaveBeenCalled();
      expect(lastPostedPayload().status).toBe('FAILED');
      const verdict = verdictWithDisabled() as VerdictWithDisabled & { setupFailed?: boolean; output: string };
      expect(verdict.setupFailed).toBe(true);
      expect(verdict.output).toContain('authoritative PR diff');
      expect(verdict.disabledTests).toBeUndefined();
    });
  });
});

describe('handleVerifyTests — declared acceptance-test probe', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const PYTEST_RUNNER = {
    cmd: 'python3', args: ['-m', 'pytest', '-x', '-q'],
    cwd: '/tmp/workspace/backend', label: 'pytest (backend)',
  };
  const declaredTask = (selector: string, kind = 'test') => ({
    context: { acceptance_evidence: { kind, descriptor: 'the seam test', selector } },
  });

  type Acc = { declared: boolean; descriptor: string; selector?: string; present: boolean; ran: boolean; status: string; passed?: boolean; in_diff?: boolean };
  function acceptanceBlock(): Acc | undefined {
    return (JSON.parse(lastPostedPayload().logs as string) as { acceptanceTest?: Acc }).acceptanceTest;
  }

  // Mock the shared PR-diff `git diff --name-only` pass to report `changedFiles`,
  // while every other execFileAsync (git rev-parse, the targeted pytest run)
  // returns `runOutput`. Lets a test control whether the declared test file is
  // among the PR's changed files (the in_diff cross-check).
  function mockDiffAndRun(changedFiles: string[], runOutput: string): void {
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
      if (cmd === 'git') {
        const a = args as string[];
        if (a.includes('--name-only')) return Promise.resolve(changedFiles.join('\n'));
        return Promise.resolve('');
      }
      return Promise.resolve(runOutput);
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(detectAutoTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([]);
    vi.mocked(discoverDocumentedCommands).mockReturnValue([]);
    vi.mocked(detectTestRunners).mockReturnValue([PYTEST_RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
  });

  it('declared test that RAN → acceptanceTest.status=ran, ran=true (no block)', async () => {
    vi.mocked(execFileAsync).mockImplementation((cmd: string) =>
      Promise.resolve(cmd === 'git' ? '' : 'collected 1 item\n1 passed in 0.10s'),
    );
    await handleVerifyTests('at1', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block).toBeDefined();
    expect(block?.status).toBe('ran');
    expect(block?.ran).toBe(true);
    expect(block?.present).toBe(true);
    expect(block?.selector).toBe('testing/test_seam.py::test_x');
    // Targeted run exited zero and classified `ran` → the declared test is GREEN.
    expect(block?.passed).toBe(true);
  });

  it('declared test that FAILED (non-zero exit, still classified ran) → passed=false', async () => {
    // The targeted runner exits non-zero (rejects) with a real test-FAILURE
    // signature (not an environment failure), so it classifies as `ran` but the
    // run is RED — passed must be false so a failing declared test cannot mint proven.
    vi.mocked(execFileAsync).mockImplementation((cmd: string) => {
      if (cmd === 'git') return Promise.resolve('');
      const err = Object.assign(new Error('pytest exited 1'), {
        stdout: 'collected 1 item\n\ntest_seam.py::test_x FAILED\n\n1 failed in 0.12s',
        stderr: '',
      });
      return Promise.reject(err);
    });
    await handleVerifyTests('at1b', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('ran');
    expect(block?.ran).toBe(true);
    expect(block?.passed).toBe(false);
  });

  it('declared test SKIPPED → status=skipped, ran=false (backend blocks)', async () => {
    vi.mocked(execFileAsync).mockImplementation((cmd: string) =>
      Promise.resolve(cmd === 'git' ? '' : 'collected 1 item\n1 skipped in 0.01s'),
    );
    await handleVerifyTests('at2', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('skipped');
    expect(block?.ran).toBe(false);
    expect(block?.present).toBe(true);
    expect(block?.passed).toBe(false);
  });

  it('declared test ABSENT → status=absent, present=false (backend blocks)', async () => {
    vi.mocked(execFileAsync).mockImplementation((cmd: string) =>
      Promise.resolve(cmd === 'git' ? '' : 'no tests ran in 0.01s'),
    );
    await handleVerifyTests('at3', payload, declaredTask('testing/test_missing.py::test_y'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('absent');
    expect(block?.present).toBe(false);
    expect(block?.ran).toBe(false);
    expect(block?.passed).toBe(false);
  });

  it('environment failure during targeted run → could_not_run (degrades, never false-absent)', async () => {
    vi.mocked(execFileAsync).mockImplementation((cmd: string) => {
      if (cmd === 'git') return Promise.resolve('');
      const err = Object.assign(new Error('boom'), { stdout: '', stderr: 'error while loading shared libraries: libatk-1.0.so.0' });
      return Promise.reject(err);
    });
    await handleVerifyTests('at4', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('could_not_run');
    expect(block?.ran).toBe(false);
    expect(block?.passed).toBe(false);
  });

  it('non-pytest / un-targetable selector → could_not_run (no execFile run)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([{ cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' }]);
    vi.mocked(execFileAsync).mockResolvedValue('');
    await handleVerifyTests('at5', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('could_not_run');
    expect(block?.passed).toBe(false);
  });

  it('declared test RAN and IS among the PR changed files → in_diff=true', async () => {
    mockDiffAndRun(['testing/test_seam.py', 'app/seam.py'], 'collected 1 item\n1 passed in 0.10s');
    await handleVerifyTests('atd1', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('ran');
    expect(block?.passed).toBe(true);
    expect(block?.in_diff).toBe(true);
  });

  it('declared test RAN but NOT among the PR changed files → in_diff=false (visible pre-merge)', async () => {
    // The declared test passes green, but the PR did not add/modify it — the
    // backend gate will hard-fail proven=False. Surface it PRE-merge.
    mockDiffAndRun(['app/seam.py', 'app/other.py'], 'collected 1 item\n1 passed in 0.10s');
    await handleVerifyTests('atd2', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('ran');
    expect(block?.passed).toBe(true);
    expect(block?.in_diff).toBe(false);
    // The operator-facing note carries the pre-merge warning.
    const logs = lastPostedPayload().logs as string;
    expect(logs).toContain('is NOT among');
  });

  it('basename-only changed path still matches the declared test (tolerant match)', async () => {
    mockDiffAndRun(['backend/testing/test_seam.py'], 'collected 1 item\n1 passed in 0.10s');
    await handleVerifyTests('atd3', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    expect(acceptanceBlock()?.in_diff).toBe(true);
  });

  it('empty changed-file list (diff unavailable) → in_diff omitted (indeterminate)', async () => {
    // git returns '' for --name-only → changedFiles=[] → indeterminate, not false.
    mockDiffAndRun([], 'collected 1 item\n1 passed in 0.10s');
    await handleVerifyTests('atd4', payload, declaredTask('testing/test_seam.py::test_x'), 'tok', 'o', 'r', null, { value: '' });
    const block = acceptanceBlock();
    expect(block?.status).toBe('ran');
    expect(block?.in_diff).toBeUndefined();
  });

  it('no acceptance evidence declared → acceptanceTest omitted entirely', async () => {
    vi.mocked(execFileAsync).mockResolvedValue('');
    await handleVerifyTests('at6', payload, {}, 'tok', 'o', 'r', null, { value: '' });
    expect(acceptanceBlock()).toBeUndefined();
  });

  it('non-test acceptance kind (behavior) → acceptanceTest omitted', async () => {
    vi.mocked(execFileAsync).mockResolvedValue('');
    await handleVerifyTests('at7', payload, declaredTask('something', 'behavior'), 'tok', 'o', 'r', null, { value: '' });
    expect(acceptanceBlock()).toBeUndefined();
  });
});

describe('handleVerifyTests — diff-aware runner filtering', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const AGENT_RUNNER = { cmd: 'npx', args: ['vitest'], cwd: `${WORKSPACE}/agent`, label: 'vitest (agent)' };
  const BACKEND_RUNNER = { cmd: 'pytest', args: [], cwd: `${WORKSPACE}/backend`, label: 'pytest (backend)' };
  const FRONTEND_RUNNER = { cmd: 'npm', args: ['run', 'build'], cwd: `${WORKSPACE}/admin-ui`, label: 'npm run build (admin-ui)' };
  const ROOT_RUNNER = { cmd: 'npm', args: ['test'], cwd: WORKSPACE, label: 'npm test (root)' };

  function mockDiff(changedFiles: string[]): void {
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
      if (cmd === 'git') {
        const a = args as string[];
        if (a.includes('--name-only')) return Promise.resolve(changedFiles.join('\n'));
        if (a.includes('--unified=0')) return Promise.resolve('');
      }
      return Promise.resolve('');
    });
  }

  function runnersUsed(): string[] {
    const calls = vi.mocked(testFixLoop).mock.calls;
    if (calls.length === 0) return [];
    const opts = calls[0][8] as { runnersOverride?: Array<{ label: string }> } | undefined;
    return (opts?.runnersOverride ?? []).map((r: { label: string }) => r.label);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });
    vi.mocked(detectAutoTestRunners).mockReturnValue([]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([]);
    vi.mocked(discoverDocumentedCommands).mockReturnValue([]);
    vi.mocked(execFileAsync).mockResolvedValue('');
  });

  it('runs only backend runner when only backend/ files changed', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER, FRONTEND_RUNNER]);
    mockDiff(['backend/app/foo.py', 'backend/testing/test_foo.py']);

    await handleVerifyTests('df1', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(runnersUsed()).toEqual([BACKEND_RUNNER.label]);
  });

  it('an authoritative empty diff runs root runners only', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([ROOT_RUNNER, AGENT_RUNNER, BACKEND_RUNNER]);
    mockDiff([]);

    await handleVerifyTests('df-empty', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(runnersUsed()).toEqual([ROOT_RUNNER.label]);
  });

  it('a diff with no matching configured stack fails safely instead of expanding', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER]);
    mockDiff(['README.md']);

    await handleVerifyTests('df-docs', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(runnersUsed()).toEqual([]);
    expect(lastPostedPayload().status).toBe('FAILED');
    const verdict = parsedVerdict() as ReturnType<typeof parsedVerdict> & { setupFailed?: boolean; output: string };
    expect(verdict.setupFailed).toBe(true);
    expect(verdict.output).toContain('No configured test runner matches the authoritative PR diff');
  });

  it('runs only agent runner when only agent/ files changed', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER, FRONTEND_RUNNER]);
    mockDiff(['agent/src/handlers/foo.ts']);

    await handleVerifyTests('df2', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(runnersUsed()).toEqual([AGENT_RUNNER.label]);
  });

  it('fails setup safely instead of running every stack when diff is unavailable', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER]);
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
      if (cmd === 'git' && (args as string[]).includes('--name-only')) {
        return Promise.reject(new Error('could not compute merge-base'));
      }
      return Promise.resolve('');
    });

    await handleVerifyTests('df3', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(runnersUsed()).toEqual([]);
    expect(lastPostedPayload().status).toBe('FAILED');
    const verdict = parsedVerdict() as ReturnType<typeof parsedVerdict> & { setupFailed?: boolean; output: string };
    expect(verdict.setupFailed).toBe(true);
    expect(verdict.output).toContain('refusing to run an unscoped all-stack test gate');
  });

  it('fetches a missing base ref before filtering a backend-only PR', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER, FRONTEND_RUNNER]);
    let revParseCalls = 0;
    vi.mocked(execFileAsync).mockImplementation((cmd: unknown, args: unknown) => {
      if (cmd !== 'git') return Promise.resolve('');
      const gitArgs = args as string[];
      if (gitArgs[0] === 'rev-parse') {
        revParseCalls += 1;
        return revParseCalls === 1
          ? Promise.reject(new Error('unknown revision origin/main'))
          : Promise.resolve('base-sha');
      }
      if (gitArgs[0] === 'fetch') return Promise.resolve('');
      if (gitArgs.includes('--name-only')) return Promise.resolve('backend/app/foo.py\nbackend/testing/test_foo.py');
      if (gitArgs.includes('--unified=0')) return Promise.resolve('');
      return Promise.resolve('');
    });

    await handleVerifyTests('df-fetch', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    expect(vi.mocked(execFileAsync)).toHaveBeenCalledWith(
      'git',
      ['fetch', 'origin', '+refs/heads/main:refs/remotes/origin/main'],
      expect.objectContaining({ cwd: '/tmp/workspace' }),
    );
    expect(revParseCalls).toBe(2);
    expect(runnersUsed()).toEqual([BACKEND_RUNNER.label]);
  });

  it('MERGE_TEST_RESULT.tests contains only activeRunners, not skipped-by-path runners', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([AGENT_RUNNER, BACKEND_RUNNER, FRONTEND_RUNNER]);
    mockDiff(['agent/src/handlers/foo.ts']);

    await handleVerifyTests('df4', payload, {}, 'tok', 'o', 'r', null, { value: '' });

    const calls = vi.mocked(persistAndPostStatus).mock.calls;
    const lastPost = calls[calls.length - 1][1];
    const verdict = JSON.parse(lastPost.logs as string) as { tests: Array<{ name: string }> };
    const testNames = verdict.tests.map(t => t.name);
    expect(testNames).toEqual([AGENT_RUNNER.label]);
    expect(testNames).not.toContain(BACKEND_RUNNER.label);
    expect(testNames).not.toContain(FRONTEND_RUNNER.label);
  });
});
