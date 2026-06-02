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
  return { ...actual, detectTestRunners: vi.fn() };
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
import { detectTestRunners } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

const RUNNER = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' };

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

function parsedVerdict(): { detected: boolean; passed: boolean; tests: unknown[]; output: string } {
  return JSON.parse(lastPostedPayload().logs as string) as {
    detected: boolean; passed: boolean; tests: unknown[]; output: string;
  };
}

describe('handleVerifyTests', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
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

  it('setup failure → detected=false so the merge gate is not blocked on infra noise', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false,
      testFailuresDetected: false,
      setupFailed: true,
      failureSummary: 'npm ci failed: registry timeout',
    });

    await handleVerifyTests('t4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    expect(lastPostedPayload().status).toBe('COMPLETED');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.output).toContain('registry timeout');
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
