import { vi, describe, it, expect, beforeEach } from 'vitest';

// Proves the durable no-CI verify-gate safety net:
//  (1) a runaway/SIGTERM full-suite run (loop.timedOut) degrades to a
//      non-blocking fall-open (detected=false, COMPLETED) and is NOT retried
//      into an infinite setupFailed crash loop; and
//  (2) a bare whole-suite `python3 -m pytest` runner is SCOPED to the
//      diff-impacted test files before it runs (bounded + fast), and when
//      nothing is impacted it falls open rather than running the whole suite.
//
// Mirrors the mock surface of the sibling verify-tests.*.test.ts files: index.js
// IO is stubbed, execFileAsync is driven per-test, and testFixLoop is mocked so
// the handler's routing (not a real test run) is under test.
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    validateBranchName: vi.fn((b: string) => b),
    execFileAsync: vi.fn().mockResolvedValue(''),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
  listWorkspaceTopDirs: vi.fn().mockReturnValue([]),
  resolveReferenceRepo: vi.fn().mockReturnValue(null),
  getLastWorkspacePrepOutcome: vi.fn().mockReturnValue(null),
  resetWorkspacePrepOutcome: vi.fn(),
}));

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue(undefined),
}));

// detectTestRunners / detectAcceptanceProbes / discoverDocumentedCommands are
// stubbed; isUnscopedPytestRunner + scopePytestRunnerToFiles stay REAL (the code
// under test) via the ...actual spread.
vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  return {
    ...actual,
    detectTestRunners: vi.fn(),
    detectAutoTestRunners: vi.fn().mockReturnValue([]),
    detectAcceptanceProbes: vi.fn().mockReturnValue([]),
    discoverDocumentedCommands: vi.fn().mockReturnValue([]),
  };
});

vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return { ...actual, testFixLoop: vi.fn() };
});

// existsSync answers true ONLY for the committed impacted-test resolver, so
// impactedResolverAvailable() is realistic (a Blueprint repo carries it).
vi.mock('fs', () => ({
  default: {
    writeFileSync: vi.fn(),
    existsSync: vi.fn((p: unknown) => typeof p === 'string' && p.endsWith('backend/testing/impacted_tests.py')),
  },
}));

import { handleVerifyTests } from './verify-tests.js';
import { persistAndPostStatus, execFileAsync } from '../index.js';
import { detectTestRunners } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

const WS = '/tmp/workspace';
const NPM_RUNNER = { cmd: 'npm', args: ['test'], cwd: WS, label: 'npm test (root)' };
// The exact wedging command: a bare whole-suite pytest rooted at backend/.
const BARE_PYTEST = { cmd: 'python3', args: ['-m', 'pytest'], cwd: `${WS}/backend`, label: 'python3 -m pytest (backend)' };

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}
function parsedVerdict(): { detected: boolean; passed: boolean; setupFailed?: boolean; tests: unknown[]; output: string } {
  return JSON.parse(lastPostedPayload().logs as string) as {
    detected: boolean; passed: boolean; setupFailed?: boolean; tests: unknown[]; output: string;
  };
}

// A computePrDiff-shaped execFileAsync: git name-only lists one changed backend
// app file (so the backend runner survives filterRunnersByDiff); the impacted
// resolver returns `resolverOut`; everything else resolves empty.
function driveExecFileAsync(resolverOut: string): void {
  vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[] = []) => {
    if (cmd === 'git' && args.includes('--name-only')) return Promise.resolve('backend/app/ci_resolution.py\n');
    if (cmd === 'python3' && args[0] === 'backend/testing/impacted_tests.py') return Promise.resolve(resolverOut);
    return Promise.resolve('');
  });
}

describe('handleVerifyTests — bounded/fall-open safety net', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(execFileAsync).mockResolvedValue('');
  });

  it('(1) a runaway full-suite run (timedOut) FALLS OPEN — COMPLETED detected=false, NOT retried, NOT FAILED', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([NPM_RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false, testFailuresDetected: false, timedOut: true,
      failureSummary: 'runner timeout: Signal: SIGTERM',
    });

    await handleVerifyTests('t1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Not retried: the wall-clock kill is terminal, unlike a transient setup flake.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
    const post = lastPostedPayload();
    expect(post.status).toBe('COMPLETED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBeUndefined();
    expect(verdict.output).toMatch(/wall-clock|could not run/i);
  });

  it('(2) a bare `python3 -m pytest` is SCOPED to the diff-impacted test files before running', async () => {
    driveExecFileAsync('backend/testing/test_ci_resolution.py\n');
    vi.mocked(detectTestRunners).mockReturnValue([BARE_PYTEST]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // The runner handed to the loop must be the SCOPED command (impacted file
    // appended, backend/ prefix stripped), not the unbounded whole suite.
    const opts = vi.mocked(testFixLoop).mock.calls[0][8] as { runnersOverride?: Array<{ args: string[] }> };
    expect(opts.runnersOverride).toHaveLength(1);
    expect(opts.runnersOverride?.[0].args).toEqual(['-m', 'pytest', 'testing/test_ci_resolution.py']);
    // The resolver was actually consulted with the changed file(s).
    const resolverCall = vi.mocked(execFileAsync).mock.calls.find(
      c => c[0] === 'python3' && Array.isArray(c[1]) && c[1][0] === 'backend/testing/impacted_tests.py',
    );
    expect(resolverCall?.[1]).toContain('backend/app/ci_resolution.py');
    expect(lastPostedPayload().status).toBe('COMPLETED');
    expect(parsedVerdict().detected).toBe(true);
    expect(parsedVerdict().passed).toBe(true);
  });

  it('(2b) an unbounded pytest runner with NO impacted files falls open — the suite never runs (no vacuous pass)', async () => {
    driveExecFileAsync('');  // resolver finds nothing impacted
    vi.mocked(detectTestRunners).mockReturnValue([BARE_PYTEST]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // The whole suite is NEVER run — we fall open instead of an unbounded run.
    expect(vi.mocked(testFixLoop)).not.toHaveBeenCalled();
    const post = lastPostedPayload();
    expect(post.status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.passed).toBe(false);
    expect(verdict.setupFailed).toBeUndefined();
  });
});
