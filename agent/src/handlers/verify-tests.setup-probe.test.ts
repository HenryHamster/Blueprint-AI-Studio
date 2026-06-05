import { vi, describe, it, expect, beforeEach } from 'vitest';

// Verifies the no-CI verify gate runs the cheap acceptance probe on the
// SETUP-FAILURE fall-open (the detected suite could not run after the bounded
// retries) and carries the probe verdict on the setupFailed artifact, so a
// genuine build break still blocks the merge while a probe that itself could
// not run stays distinguishable. Kept in its own file to avoid a parallel-edit
// collision with the sibling-owned verify-tests.test.ts.
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    validateBranchName: vi.fn((b: string) => b),
    execFileAsync: vi.fn(),
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
  return {
    ...actual,
    detectTestRunners: vi.fn(),
    detectProvisioningSteps: vi.fn().mockReturnValue([]),
    detectAcceptanceProbes: vi.fn(),
  };
});

vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return { ...actual, testFixLoop: vi.fn() };
});

vi.mock('fs', () => ({ default: { writeFileSync: vi.fn() } }));

import { handleVerifyTests } from './verify-tests.js';
import { persistAndPostStatus, execFileAsync } from '../index.js';
import { detectTestRunners, detectAcceptanceProbes } from '../utils/test-runners.js';
import { testFixLoop } from '../utils/test-fix-loop.js';

const RUNNER = { cmd: 'npm', args: ['test'], cwd: '/tmp/workspace', label: 'npm test (root)' };
const PROBE = {
  cmd: 'python3', args: ['-m', 'compileall', '-q', '/tmp/workspace'],
  cwd: '/tmp/workspace', ecosystem: 'python' as const, label: 'python3 -m compileall (root)',
};

function lastVerdict(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  const payload = calls[calls.length - 1][1];
  return JSON.parse(payload.logs as string) as Record<string, unknown>;
}

describe('handleVerifyTests — acceptance probe on setup-failure fall-open', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    // Every suite run fails setup (initial + bounded retries), so the gate
    // reaches the setup-failure fall-open with zero suite execution signal.
    vi.mocked(testFixLoop).mockResolvedValue({
      passed: false, testFailuresDetected: false, setupFailed: true,
      failureSummary: 'npm ci failed: registry timeout',
    });
    vi.mocked(detectAcceptanceProbes).mockReturnValue([PROBE]);
  });

  it('runs the probe; a CLEAR build failure is carried on the setupFailed artifact (blocks)', async () => {
    // Probe ran and the changed code does not compile — a real SyntaxError.
    vi.mocked(execFileAsync).mockRejectedValue(
      Object.assign(new Error('SyntaxError: invalid syntax'), { stdout: '', stderr: '' }),
    );

    await handleVerifyTests('p1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = lastVerdict();
    expect(verdict.setupFailed).toBe(true);
    const probe = verdict.probe as Record<string, unknown>;
    expect(probe).toBeDefined();
    expect(probe.ran).toBe(true);
    expect(probe.outcome).toBe('fail');
    expect(probe.passed).toBe(false);
    expect(probe.could_not_run).toBe(false);
  });

  it('a probe that itself could not run stays distinguishable (keeps fall-open)', async () => {
    // Same environment problem blocks the probe — missing dependency, not a
    // genuine build break.
    vi.mocked(execFileAsync).mockRejectedValue(
      Object.assign(new Error("ModuleNotFoundError: No module named 'foo'"), { stdout: '', stderr: '' }),
    );

    await handleVerifyTests('p2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    const verdict = lastVerdict();
    expect(verdict.setupFailed).toBe(true);
    const probe = verdict.probe as Record<string, unknown>;
    expect(probe.ran).toBe(true);
    expect(probe.could_not_run).toBe(true);
    expect(probe.passed).toBe(false);
    // Distinguishable from a clean build failure.
    expect(probe.outcome).toBe('could_not_run');
  });
});
