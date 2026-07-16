import { beforeEach, describe, it, expect, vi } from 'vitest';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/capability-proof-workspace',
    execFileAsync: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    resolveBaseBranch: vi.fn().mockReturnValue('main'),
    sendLog: vi.fn(),
    validateBranchName: vi.fn((branch: string) => branch),
  };
});

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  return {
    ...actual,
    isDockerAvailable: vi.fn().mockReturnValue(false),
  };
});

import {
  buildCapabilityProofTrialRunner,
  handleCapabilityProofTrial,
  isRefSetTargetable,
  parseTrialSkippedSummary,
} from './capability-proof-trial.js';
import { isDockerAvailable } from '../utils/test-runners.js';
import { execFileAsync, persistAndPostStatus } from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';

const WS = '/tmp/ws';

// Deterministic Docker-availability stubs for buildCapabilityProofTrialRunner tests.
const dockerAvailable = () => true;
const dockerUnavailable = () => false;

describe('buildCapabilityProofTrialRunner', () => {
  it('targets Python refs via scripts/test-backend.sh with --postgres when Docker available', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['backend/testing/test_foo.py', 'backend/testing/test_bar.py'],
      WS,
      dockerAvailable,
    );
    expect(runner).toEqual({
      cmd: 'bash',
      cwd: WS,
      args: ['scripts/test-backend.sh', '--postgres', 'testing/test_foo.py', 'testing/test_bar.py'],
    });
  });

  it('uses --no-postgres when Docker is unavailable (prevents env-failure on Docker-less VMs)', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['backend/testing/test_foo.py', 'backend/testing/test_bar.py'],
      WS,
      dockerUnavailable,
    );
    expect(runner).toEqual({
      cmd: 'bash',
      cwd: WS,
      args: ['scripts/test-backend.sh', '--no-postgres', 'testing/test_foo.py', 'testing/test_bar.py'],
    });
  });

  it('preserves a pytest ::node selector while stripping backend/ prefix', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['backend/testing/test_foo.py::test_x'],
      WS,
      dockerUnavailable,
    );
    expect(runner).toEqual({
      cmd: 'bash',
      cwd: WS,
      args: ['scripts/test-backend.sh', '--no-postgres', 'testing/test_foo.py::test_x'],
    });
  });

  it('targets TS refs via npx vitest run from the owning package dir', () => {
    const runner = buildCapabilityProofTrialRunner(
      ['agent/src/handlers/foo.test.ts', 'agent/src/utils/bar.test.ts'],
      WS,
    );
    expect(runner).toEqual({
      cmd: 'npx',
      cwd: `${WS}/agent`,
      args: ['vitest', 'run', 'src/handlers/foo.test.ts', 'src/utils/bar.test.ts'],
    });
  });

  it('returns null for an empty ref set (mints nothing)', () => {
    expect(buildCapabilityProofTrialRunner([], WS)).toBeNull();
    expect(buildCapabilityProofTrialRunner(['', '   '], WS)).toBeNull();
  });

  it('returns null for an unknown extension (cannot target cleanly)', () => {
    expect(buildCapabilityProofTrialRunner(['docs/readme.md'], WS)).toBeNull();
  });

  it('returns null for a mixed-language ref set', () => {
    expect(
      buildCapabilityProofTrialRunner(['backend/testing/test_a.py', 'agent/src/b.test.ts'], WS),
    ).toBeNull();
  });

  it('returns null for a cross-package TS ref set (no single invocation)', () => {
    expect(
      buildCapabilityProofTrialRunner(['agent/src/a.test.ts', 'admin-ui/src/b.test.ts'], WS),
    ).toBeNull();
  });
});

describe('isRefSetTargetable', () => {
  it('returns true for a Python ref set', () => {
    expect(isRefSetTargetable(['backend/testing/test_foo.py'])).toBe(true);
    expect(isRefSetTargetable(['testing/test_a.py', 'testing/test_b.py'])).toBe(true);
  });

  it('returns true for TS refs within the same package', () => {
    expect(isRefSetTargetable(['agent/src/a.test.ts', 'agent/src/b.test.ts'])).toBe(true);
    expect(isRefSetTargetable(['admin-ui/src/c.test.tsx'])).toBe(true);
  });

  it('returns false for an empty ref set', () => {
    expect(isRefSetTargetable([])).toBe(false);
    expect(isRefSetTargetable(['', '   '])).toBe(false);
  });

  it('returns false for an unknown extension', () => {
    expect(isRefSetTargetable(['docs/readme.md'])).toBe(false);
  });

  it('returns false for a mixed-language set', () => {
    expect(isRefSetTargetable(['backend/testing/test_a.py', 'agent/src/b.test.ts'])).toBe(false);
  });

  it('returns false for cross-package TS refs', () => {
    expect(isRefSetTargetable(['agent/src/a.test.ts', 'admin-ui/src/b.test.ts'])).toBe(false);
  });
});

describe('parseTrialSkippedSummary', () => {
  it('parses passed/skipped/failed counts into total+skipped', () => {
    expect(parseTrialSkippedSummary('5 passed, 2 skipped in 1.2s')).toEqual({ total: 7, skipped: 2 });
    expect(parseTrialSkippedSummary('3 passed, 1 failed')).toEqual({ total: 4, skipped: 0 });
  });

  it('returns undefined when no counts are parseable (do not fabricate)', () => {
    expect(parseTrialSkippedSummary('')).toBeUndefined();
    expect(parseTrialSkippedSummary('some unrelated output')).toBeUndefined();
  });
});

describe('handleCapabilityProofTrial Docker-flag propagation', () => {
  const pyPayload = {
    cap: 'repo-map',
    head_sha: 'queued-sha',
    test_refs: ['backend/testing/test_capability_proof.py'],
  };
  const statusRef = { value: '' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('passes --no-postgres to test-backend.sh when Docker is unavailable', async () => {
    vi.mocked(isDockerAvailable).mockReturnValue(false);
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('executed-sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await handleCapabilityProofTrial(
      'task-docker-1', pyPayload, {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const runnerCall = vi.mocked(execFileAsync).mock.calls[1];
    expect(runnerCall[0]).toBe('bash');
    expect(runnerCall[1]).toContain('--no-postgres');
    expect(runnerCall[1]).not.toContain('--postgres');
  });

  it('passes --postgres to test-backend.sh when Docker is available', async () => {
    vi.mocked(isDockerAvailable).mockReturnValue(true);
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('executed-sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await handleCapabilityProofTrial(
      'task-docker-2', pyPayload, {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const runnerCall = vi.mocked(execFileAsync).mock.calls[1];
    expect(runnerCall[0]).toBe('bash');
    expect(runnerCall[1]).toContain('--postgres');
    expect(runnerCall[1]).not.toContain('--no-postgres');
  });

  it('strips the backend/ prefix from Python refs in the runner args', async () => {
    vi.mocked(isDockerAvailable).mockReturnValue(false);
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('executed-sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await handleCapabilityProofTrial(
      'task-docker-3', pyPayload, {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const runnerCall = vi.mocked(execFileAsync).mock.calls[1];
    expect(runnerCall[1]).toContain('testing/test_capability_proof.py');
    expect(runnerCall[1].some(a => a.startsWith('backend/'))).toBe(false);
  });
});

describe('handleCapabilityProofTrial checkout provenance', () => {
  const payload = {
    cap: 'repo-map',
    head_sha: 'queued-sha',
    test_refs: ['agent/src/handlers/capability-proof-trial.test.ts'],
  };
  const statusRef = { value: '' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  async function runTrial(): Promise<Record<string, unknown>> {
    await handleCapabilityProofTrial(
      'task-1', payload, {}, 'token', 'owner', 'repo', null, statusRef,
    );
    const report = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    return JSON.parse(report.logs as string) as Record<string, unknown>;
  }

  it('reports the checked-out SHA while retaining a different queued SHA', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('executed-sha\n')
      .mockResolvedValueOnce('1 passed\n');

    const result = await runTrial();

    expect(execFileAsync).toHaveBeenNthCalledWith(
      1, 'git', ['rev-parse', 'HEAD'], { cwd: '/tmp/capability-proof-workspace' },
    );
    expect(result).toMatchObject({
      head_sha: 'executed-sha', queued_head_sha: 'queued-sha', ran: true, passed: true,
    });
  });

  it('reports an indeterminate SHA when rev-parse fails without changing run classification', async () => {
    vi.mocked(execFileAsync)
      .mockRejectedValueOnce(new Error('git unavailable'))
      .mockResolvedValueOnce('1 passed\n');

    const result = await runTrial();

    expect(result).toMatchObject({
      head_sha: null, queued_head_sha: 'queued-sha', ran: true, passed: true, could_not_run: false,
    });
  });
});
