import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mirrors verify-tests.test.ts mock surface. Kept in a separate file (rather
// than added to verify-tests.test.ts) to avoid a parallel-edit collision with
// the sibling task that owns that file. Focuses on the transient-setup-failure
// retry path added by "fix: retry transient no-CI verifier setup flake".
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    validateBranchName: vi.fn((b: string) => b),
    // execFileAsync drives the diff computation and acceptance probes; mocked
    // so tests never shell out to git/filesystem operations. Avoids ~3.2s per
    // call which would hit the 5s vitest timeout under CI contention.
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
  return { ...actual, detectTestRunners: vi.fn() };
});

vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return { ...actual, captureTestBaseline: vi.fn().mockResolvedValue({ records: [] }), testFixLoop: vi.fn() };
});

vi.mock('fs', () => ({
  default: {
    writeFileSync: vi.fn(),
  },
}));

import { handleVerifyTests } from './verify-tests.js';
import { execFileAsync, persistAndPostStatus } from '../index.js';
import { detectTestRunners } from '../utils/test-runners.js';
import { captureTestBaseline, testFixLoop } from '../utils/test-fix-loop.js';

const RUNNER = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' };

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

function parsedVerdict(): { detected: boolean; passed: boolean; setupFailed?: boolean; verificationUnverified?: boolean; tests: unknown[]; output: string } {
  return JSON.parse(lastPostedPayload().logs as string) as {
    detected: boolean; passed: boolean; setupFailed?: boolean; verificationUnverified?: boolean; tests: unknown[]; output: string;
  };
}

describe('handleVerifyTests — transient setup-failure retry', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(captureTestBaseline).mockResolvedValue({ records: [] });
    vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[] = []) =>
      Promise.resolve(cmd === 'git' && args[0] === 'rev-parse' && args.includes('HEAD') ? 'head-sha' : ''),
    );
  });

  it('setup fails once then the suite runs → uses the REAL verdict (detected=true), not detected=false', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({
        passed: false, testFailuresDetected: false, setupFailed: true,
        failureSummary: 'npm ci failed: registry timeout',
      })
      .mockResolvedValueOnce({ passed: true, testFailuresDetected: false });

    await handleVerifyTests('s1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Re-ran the loop after the transient flake.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(2);
    const post = lastPostedPayload();
    expect(post.status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(true);
    expect(verdict.setupFailed).toBeUndefined();
  });

  it('setup fails once then the suite runs and FAILS → detected=true, passed=false (failure is not masked by the retry)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop)
      .mockResolvedValueOnce({
        passed: false, testFailuresDetected: false, setupFailed: true,
        failureSummary: 'runner timeout',
      })
      .mockResolvedValueOnce({
        passed: false, testFailuresDetected: true,
        failureSummary: 'AssertionError: expected 1 to equal 2',
      });

    await handleVerifyTests('s2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(true);
    expect(verdict.passed).toBe(false);
    expect(verdict.output).toContain('AssertionError');
  });

  it('setup fails on every attempt → FAILED with setupFailed marker, retries are bounded', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false, testFailuresDetected: false, setupFailed: true,
      failureSummary: 'npm ci failed: registry timeout',
    });

    await handleVerifyTests('s3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Bounded: 1 initial run + MAX_VERIFY_SETUP_RETRIES (2) retries = 3 total.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(3);
    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBe(true);
  });

  it('unverifiable baseline attribution → artifact carries verificationUnverified (backend blocks on it)', async () => {
    // The structured baseline could not attribute failures (no structured
    // report / comparator unavailable). This is DETERMINISTIC — every verify
    // re-spawn reproduces it — so the artifact MUST carry the
    // verificationUnverified marker that ci_resolution maps to a merge BLOCK.
    // Without the marker the backend sees a FAILED task with no verdict, treats
    // it as a crash, and merges UNVERIFIED once the retries are exhausted.
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false, testFailuresDetected: false, verificationUnverified: true,
      failureSummary: 'Test runner failed but did not produce a structured failure report for baseline attribution.',
    });

    await handleVerifyTests('s5', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Not retried as a setup flake: the condition is deterministic.
    expect(vi.mocked(testFixLoop)).toHaveBeenCalledTimes(1);
    const post = lastPostedPayload();
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict();
    expect(verdict.verificationUnverified).toBe(true);
    // Distinct from the setup-failure lane, which owns its own operator label.
    expect(verdict.setupFailed).toBeUndefined();
    expect(verdict.output).toContain('structured failure report');
  });

  it('genuine no-suite repo still falls open (detected=false, COMPLETED) — forward progress preserved', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);

    await handleVerifyTests('s4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(vi.mocked(testFixLoop)).not.toHaveBeenCalled();
    const post = lastPostedPayload();
    expect(post.status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBeUndefined();
  });
});
