import { vi, describe, it, expect, beforeEach } from 'vitest';

// Verifies the no-CI verify gate provisions declared / auto-detected
// dependencies BEFORE the detected suite and the declared build probe, and that
// a provisioning failure routes through the EXISTING setup-failure path (FAILED
// + setupFailed) rather than the code-fix loop or a silent pass. Kept in its own
// file to avoid a parallel-edit collision with the sibling-owned
// verify-tests.test.ts.

// Records call ordering across execFileAsync (provisioning + probe) and the
// suite loop so the "ahead of" assertions can check sequencing.
const callLog: string[] = [];

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    captureTestBaseline: vi.fn().mockResolvedValue({ records: [] }),
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
  resolveBuildConfig: vi.fn().mockReturnValue({
    directories: [{ path: '', type: 'python', install_cmd: 'make setup' }],
  }),
}));

vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  return {
    ...actual,
    detectTestRunners: vi.fn(),
    detectProvisioningSteps: vi.fn(),
    detectAcceptanceProbes: vi.fn().mockReturnValue([]),
  };
});

vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return {
    ...actual,
    captureTestBaseline: vi.fn().mockResolvedValue({ records: [] }),
    testFixLoop: vi.fn(() => {
      callLog.push('suite');
      return Promise.resolve({ passed: true, testFailuresDetected: false });
    }),
  };
});

vi.mock('fs', () => ({ default: { writeFileSync: vi.fn() } }));

import { handleVerifyTests } from './verify-tests.js';
import { persistAndPostStatus, execFileAsync } from '../index.js';
import { detectTestRunners, detectProvisioningSteps, detectAcceptanceProbes } from '../utils/test-runners.js';
import { captureTestBaseline } from '../utils/test-fix-loop.js';

const RUNNER = { cmd: 'python3', args: ['-m', 'pytest'], cwd: '/tmp/workspace', label: 'pytest (root)' };
const INSTALL_STEP = { cmd: 'make', args: ['setup'], cwd: '/tmp/workspace', label: 'make setup (root)', declared: true };

function lastPostedPayload(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}
function parsedVerdict(): { detected: boolean; passed: boolean; setupFailed?: boolean } {
  return JSON.parse(lastPostedPayload().logs as string) as { detected: boolean; passed: boolean; setupFailed?: boolean };
}

describe('handleVerifyTests — dependency provisioning', () => {
  const payload = { branch: 'feature/x', base_branch: 'main' };
  const task = {};

  beforeEach(() => {
    vi.clearAllMocks();
    callLog.length = 0;
    vi.mocked(detectProvisioningSteps).mockReturnValue([INSTALL_STEP]);
    vi.mocked(detectAcceptanceProbes).mockReturnValue([]);
    vi.mocked(captureTestBaseline).mockResolvedValue({ records: [] });
  });

  it('runs the declared install AHEAD of the detected suite', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[] = []) => {
      callLog.push(`exec:${cmd}`);
      return Promise.resolve(cmd === 'git' && args[0] === 'rev-parse' ? 'head-sha' : '');
    });

    await handleVerifyTests('p1', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // The documented-command probe appends git-diff calls on the COMPLETED
    // path; filter them out — this test asserts provisioning precedes the suite.
    expect(callLog.filter(c => !c.startsWith('exec:git'))).toEqual(['exec:make', 'suite']);
    expect(lastPostedPayload().status).toBe('COMPLETED');
    expect(parsedVerdict().detected).toBe(true);
  });

  it('runs the declared install AHEAD of the build probe (no-suite repo)', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([]);
    // A probe in the workspace; both provisioning and the probe use execFileAsync.
    vi.mocked(detectAcceptanceProbes).mockReturnValue([
      { cmd: 'python3', args: ['-m', 'compileall'], cwd: '/tmp/workspace', ecosystem: 'python', label: 'compileall (root)' },
    ]);
    vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[]) => {
      callLog.push(`exec:${cmd} ${args.join(' ')}`);
      return Promise.resolve(cmd === 'git' && args[0] === 'rev-parse' ? 'head-sha' : '');
    });

    await handleVerifyTests('p2', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Provisioning (`make setup`) precedes the probe (`python3 -m compileall`).
    expect(callLog[0]).toBe('exec:make setup');
    expect(callLog).toContain('exec:python3 -m compileall');
    expect(callLog.indexOf('exec:make setup')).toBeLessThan(callLog.indexOf('exec:python3 -m compileall'));
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });

  it('declared-install failure routes to the setup-failure path, not the suite', async () => {
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(execFileAsync).mockImplementation(() =>
      Promise.reject(Object.assign(new Error('make: *** [setup] Error 2'), { stderr: 'pip resolver error' })),
    );

    await handleVerifyTests('p3', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Suite never ran — provisioning gated it.
    expect(callLog).not.toContain('suite');
    const post = lastPostedPayload();
    expect(post.status).toBe('FAILED');
    expect(post.artifact_type).toBe('MERGE_TEST_RESULT');
    const verdict = parsedVerdict();
    expect(verdict.detected).toBe(false);
    expect(verdict.setupFailed).toBe(true);
    // Bounded retry: 1 initial + MAX_VERIFY_SETUP_RETRIES (2) = 3 attempts.
    expect(vi.mocked(execFileAsync)).toHaveBeenCalledTimes(3);
  });

  it('no provisioning steps → suite runs unchanged (no-op pass-through)', async () => {
    vi.mocked(detectProvisioningSteps).mockReturnValue([]);
    vi.mocked(detectTestRunners).mockReturnValue([RUNNER]);
    vi.mocked(execFileAsync).mockImplementation((cmd: string, args: string[] = []) => {
      callLog.push(`exec:${cmd}`);
      return Promise.resolve(cmd === 'git' && args[0] === 'rev-parse' ? 'head-sha' : '');
    });

    await handleVerifyTests('p4', payload, task, 'tok', 'o', 'r', null, { value: '' });

    // Filter out the documented-command probe's git-diff calls (COMPLETED path).
    expect(callLog.filter(c => !c.startsWith('exec:git'))).toEqual(['suite']);
    expect(lastPostedPayload().status).toBe('COMPLETED');
  });
});
