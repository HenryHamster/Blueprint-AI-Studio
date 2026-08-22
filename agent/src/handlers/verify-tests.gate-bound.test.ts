import { vi, describe, it, expect, beforeEach } from 'vitest';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

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
vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  const actualDefault = (actual as typeof actual & { default?: typeof actual }).default ?? actual;
  return {
    ...actual,
    default: {
      ...actualDefault,
      writeFileSync: vi.fn(),
      existsSync: vi.fn((p: unknown) => typeof p === 'string' && p.endsWith('backend/testing/impacted_tests.py')),
    },
  };
});

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
    if (cmd === 'python3' && args[0] === 'backend/testing/impacted_tests.py') {
      const mandatoryIndex = args.indexOf('--mandatory-output');
      writeFileSync(args[mandatoryIndex + 1], '');
      return Promise.resolve(resolverOut);
    }
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

  it('(2a) retains a matched surface sentinel past eight ordinary impacted selections', async () => {
    const ordinary = Array.from(
      { length: 8 },
      (_, index) => `backend/testing/test_ordinary_${index + 1}.py`,
    );
    const sentinel = 'backend/testing/test_drift_sentinel.py';
    vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[] = []) => {
      if (cmd === 'git' && args.includes('--name-only')) return Promise.resolve('backend/app/ci_resolution.py\n');
      if (cmd === 'python3' && args[0] === 'backend/testing/impacted_tests.py') {
        const mandatoryIndex = args.indexOf('--mandatory-output');
        expect(args).toContain('--limit');
        expect(args[args.indexOf('--limit') + 1]).toBe('8');
        expect(mandatoryIndex).toBeGreaterThan(-1);
        writeFileSync(args[mandatoryIndex + 1], `${sentinel}\n`);
        return Promise.resolve(`${ordinary.join('\n')}\n`);
      }
      return Promise.resolve('');
    });
    vi.mocked(detectTestRunners).mockReturnValue([BARE_PYTEST]);
    vi.mocked(testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('t2a', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const opts = vi.mocked(testFixLoop).mock.calls[0][8] as { runnersOverride?: Array<{ args: string[] }> };
    expect(opts.runnersOverride?.[0].args).toEqual([
      '-m', 'pytest',
      ...ordinary.map(path => path.replace('backend/', '')),
      sentinel.replace('backend/', ''),
    ]);
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

  it('(3) the publication backend lane records route-keyed baseline attribution', () => {
    const verifier = readFileSync(
      fileURLToPath(new URL('../prompts/check_backend_verification.sh', import.meta.url)),
      'utf8',
    );
    // The production lane must attribute both successful and failed JUnit runs
    // through the shared route-aware guard; a rendered traceback comparison is
    // not an acceptable substitute for these structured fields.
    expect(verifier).toContain('--current-exit-code "$_verification_exit"');
    // The structured attribution reaches the durable sidecar through the real
    // scripts/verification_receipt.py writer, which MERGES this lane's block
    // beside the other attributed lanes instead of overwriting the file.
    expect(verifier).toContain(
      'python3 scripts/verification_receipt.py backend-pytest --receipt "$_VERIFY_RECEIPT"',
    );
    // Passed as a FILE, not argv: one traceback per suppressed baseline failure
    // would otherwise blow the OS argv-size limit (E2BIG) on a busy run.
    expect(verifier).toContain('--attribution-file "$_verification_attribution_file"');
    // The fragment is emitted for any blueprint-v1 project, so the writer is
    // reached behind a readability guard with a self-contained fallback -- a
    // foreign workspace without it must not be blocked on a green lane.
    expect(verifier).toContain('if [ -r scripts/verification_receipt.py ]; then');
    expect(verifier).toContain('elif python3 -c ');
    const writer = readFileSync(
      fileURLToPath(new URL('../../../scripts/verification_receipt.py', import.meta.url)),
      'utf8',
    );
    for (const field of [
      'new_failures',
      'suppressed_failures',
      'disappeared_failures',
      'skipped_failures',
    ]) {
      expect(writer).toContain(`attribution.get("${field}")`);
    }
  });
});
