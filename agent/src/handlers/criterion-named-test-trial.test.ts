import { afterEach, beforeEach, describe, it, expect, vi, type MockInstance } from 'vitest';
import fs from 'fs';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/criterion-named-test-workspace',
    execFileAsync: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    resolveBaseBranch: vi.fn().mockReturnValue('main'),
    sendLog: vi.fn(),
    validateBranchName: vi.fn((branch: string) => branch),
  };
});

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return {
    ...actual,
    prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
  };
});

vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  return {
    ...actual,
    isDockerAvailable: vi.fn().mockReturnValue(false),
  };
});

import { handleCriterionNamedTestTrial } from './criterion-named-test-trial.js';
import { isDockerAvailable } from '../utils/test-runners.js';
import { execFileAsync, persistAndPostStatus } from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';

const WS = '/tmp/criterion-named-test-workspace';

// Build the completion payload the backend enrollment sends.
function payloadFor(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    criterion_key: 'speed-of-iteration',
    head_sha: 'pinned-sha',
    test_refs: ['backend/testing/test_foo.py'],
    ...overrides,
  };
}

async function runTrial(
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const statusRef = { value: '' };
  await handleCriterionNamedTestTrial(
    'task-1', payload, {}, 'token', 'owner', 'repo', null, statusRef,
  );
  const report = vi.mocked(persistAndPostStatus).mock.calls[0][1];
  return JSON.parse(report.logs as string) as Record<string, unknown>;
}

describe('handleCriterionNamedTestTrial could_not_run (pre-clone)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('reports could_not_run when criterion_key is missing', async () => {
    const result = await runTrial(payloadFor({ criterion_key: '' }));

    expect(result).toEqual({
      type: 'criterion_named_test_trial',
      criterion_key: '',
      head_sha: null, queued_head_sha: 'pinned-sha', test_refs: ['backend/testing/test_foo.py'],
      ran: false, passed: false, could_not_run: true,
      could_not_run_reason: 'criterion_identity_missing',
    });
    expect(execFileAsync).not.toHaveBeenCalled();
  });

  it('reports could_not_run for an empty ref set', async () => {
    const result = await runTrial(payloadFor({ test_refs: [] }));

    expect(result).toMatchObject({ ran: false, could_not_run: true });
    expect(execFileAsync).not.toHaveBeenCalled();
  });

  it('reports could_not_run for an untargetable (mixed-language) ref set', async () => {
    const result = await runTrial(payloadFor({
      test_refs: ['backend/testing/test_a.py', 'agent/src/b.test.ts'],
    }));

    expect(result).toMatchObject({ ran: false, could_not_run: true });
    expect(execFileAsync).not.toHaveBeenCalled();
  });
});

describe('handleCriterionNamedTestTrial pinned-SHA checkout', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('pins the checkout to the queued SHA and reports it as the executed head_sha', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')          // git fetch origin pinned-sha
      .mockResolvedValueOnce('HEAD is now at pinned-sha\n')  // git checkout pinned-sha
      .mockResolvedValueOnce('pinned-sha\n')  // git rev-parse HEAD
      .mockResolvedValueOnce('1 passed\n');   // bash scripts/test-backend.sh ...

    const result = await runTrial(payloadFor({}));

    expect(execFileAsync).toHaveBeenNthCalledWith(
      1, 'git', ['fetch', 'origin', 'pinned-sha'], { cwd: WS },
    );
    expect(execFileAsync).toHaveBeenNthCalledWith(
      2, 'git', ['checkout', 'pinned-sha'], { cwd: WS },
    );
    expect(execFileAsync).toHaveBeenNthCalledWith(
      3, 'git', ['rev-parse', 'HEAD'], { cwd: WS },
    );
    expect(result).toMatchObject({
      head_sha: 'pinned-sha', queued_head_sha: 'pinned-sha',
      ran: true, passed: true, could_not_run: false,
    });
  });

  it('proceeds when fetch fails but the pinned SHA is already present locally', async () => {
    vi.mocked(execFileAsync)
      .mockRejectedValueOnce(new Error('fatal: could not fetch'))
      .mockResolvedValueOnce('HEAD is now at pinned-sha\n')
      .mockResolvedValueOnce('pinned-sha\n')
      .mockResolvedValueOnce('1 passed\n');

    const result = await runTrial(payloadFor({}));

    expect(result).toMatchObject({ head_sha: 'pinned-sha', ran: true, passed: true });
  });

  it('reports could_not_run and runs no tests when the pinned SHA cannot be resolved', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockRejectedValueOnce(new Error('fatal: reference is not a tree: deadbeef'));

    const result = await runTrial(payloadFor({ head_sha: 'deadbeef' }));

    expect(result).toEqual({
      type: 'criterion_named_test_trial',
      criterion_key: 'speed-of-iteration',
      head_sha: null, queued_head_sha: 'deadbeef', test_refs: ['backend/testing/test_foo.py'],
      ran: false, passed: false, could_not_run: true,
      could_not_run_reason: 'candidate_unavailable',
    });
    // Only the git pin operations were attempted — never a test run.
    const commands = vi.mocked(execFileAsync).mock.calls.map(c => c[0]);
    expect(commands).toEqual(['git', 'git']);
  });

  it('runs at the base-branch head when no head_sha is queued (no pin attempted)', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('base-sha\n')   // git rev-parse HEAD
      .mockResolvedValueOnce('1 passed\n');

    const result = await runTrial(payloadFor({ head_sha: '' }));

    const gitCommands = vi.mocked(execFileAsync).mock.calls
      .filter(c => c[0] === 'git')
      .map(c => c[1]);
    expect(gitCommands).toEqual([['rev-parse', 'HEAD']]); // no fetch/checkout
    expect(result).toMatchObject({
      head_sha: 'base-sha', queued_head_sha: null, ran: true, passed: true,
    });
  });

  it('reports an indeterminate SHA when rev-parse fails without changing run classification', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('HEAD is now at pinned-sha\n')
      .mockRejectedValueOnce(new Error('git unavailable'))
      .mockResolvedValueOnce('1 passed\n');

    const result = await runTrial(payloadFor({}));

    expect(result).toMatchObject({
      head_sha: null, queued_head_sha: 'pinned-sha', ran: true, passed: true, could_not_run: false,
    });
  });
});

describe('handleCriterionNamedTestTrial runner selection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('targets .py refs via scripts/test-backend.sh with --no-postgres when Docker is unavailable', async () => {
    vi.mocked(isDockerAvailable).mockReturnValue(false);
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await runTrial(payloadFor({ test_refs: ['backend/testing/test_criterion.py'] }));

    const runnerCall = vi.mocked(execFileAsync).mock.calls[3];
    expect(runnerCall[0]).toBe('bash');
    expect(runnerCall[1]).toEqual([
      'scripts/test-backend.sh', '--no-postgres', 'testing/test_criterion.py',
    ]);
  });

  it('passes --postgres to test-backend.sh when Docker is available', async () => {
    vi.mocked(isDockerAvailable).mockReturnValue(true);
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await runTrial(payloadFor({ test_refs: ['backend/testing/test_criterion.py'] }));

    const runnerCall = vi.mocked(execFileAsync).mock.calls[3];
    expect(runnerCall[1]).toContain('--postgres');
    expect(runnerCall[1]).not.toContain('--no-postgres');
  });

  it('targets TS refs via npx vitest run from the owning package dir', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockResolvedValueOnce('1 passed\n');

    await runTrial(payloadFor({ test_refs: ['agent/src/handlers/foo.test.ts'] }));

    const runnerCall = vi.mocked(execFileAsync).mock.calls[3];
    expect(runnerCall[0]).toBe('npx');
    expect(runnerCall[1]).toEqual(['vitest', 'run', 'src/handlers/foo.test.ts']);
  });
});

describe('handleCriterionNamedTestTrial classification + receipt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('classifies a green run as ran/passed with the exact receipt shape', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockResolvedValueOnce('3 passed, 1 skipped in 1.2s\n');

    const result = await runTrial(payloadFor({
      test_refs: ['agent/src/handlers/foo.test.ts'],
    }));

    expect(result).toEqual({
      type: 'criterion_named_test_trial',
      criterion_key: 'speed-of-iteration',
      head_sha: 'sha',
      queued_head_sha: 'pinned-sha',
      test_refs: ['agent/src/handlers/foo.test.ts'],
      ran: true,
      passed: true,
      skipped_summary: { total: 4, skipped: 1 },
      could_not_run: false,
    });
  });

  it('degrades an environment/setup failure to could_not_run (never a red verdict)', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockRejectedValueOnce(Object.assign(new Error('spawn bash ENOENT'), { stderr: '' }));

    const result = await runTrial(payloadFor({}));

    expect(result).toMatchObject({
      ran: false, passed: false, could_not_run: true,
      could_not_run_reason: 'execution_environment_unavailable',
    });
  });

  it('echoes stable retry identities without turning could_not_run into success', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockRejectedValueOnce(Object.assign(new Error('spawn bash ENOENT'), { stderr: '' }));

    const result = await runTrial(payloadFor({
      trial_identity: 'trial-v1',
      test_identity: 'tests-v1',
      precondition_identity: 'runner-v1',
    }));

    expect(result).toMatchObject({
      trial_identity: 'trial-v1',
      test_identity: 'tests-v1',
      precondition_identity: 'runner-v1',
      ran: false,
      passed: false,
      could_not_run: true,
    });
  });

  it('reports a red (non-zero-exit) run as ran but not passed — fail-open, nothing minted', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockRejectedValueOnce(Object.assign(
        new Error('command exited with code 1'), { stdout: '1 failed, 0 passed', stderr: '' },
      ));

    const result = await runTrial(payloadFor({}));

    expect(result).toMatchObject({ ran: true, passed: false, could_not_run: false });
  });
});

describe('handleCriterionNamedTestTrial TS/JS dependency provisioning', () => {
  let existsSyncSpy: MockInstance;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
    // Simulate: package.json exists but pnpm-lock.yaml and package-lock.json do not.
    existsSyncSpy = vi.spyOn(fs, 'existsSync').mockImplementation((p: unknown) => {
      const path = String(p);
      return path.endsWith('/package.json');
    });
  });

  afterEach(() => {
    existsSyncSpy.mockRestore();
  });

  it('runs npm install before vitest for TS refs (bare npm install, no lockfile)', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')            // git fetch origin pinned-sha
      .mockResolvedValueOnce('checked-out\n')   // git checkout pinned-sha
      .mockResolvedValueOnce('sha\n')           // git rev-parse HEAD
      .mockResolvedValueOnce('')                // npm install (provisioning)
      .mockResolvedValueOnce('1 passed\n');     // npx vitest run

    await runTrial(payloadFor({ test_refs: ['agent/src/handlers/foo.test.ts'] }));

    const calls = vi.mocked(execFileAsync).mock.calls;
    expect(calls[3][0]).toBe('npm');
    expect(calls[3][1]).toContain('install');
    expect(calls[4][0]).toBe('npx');
    expect(calls[4][1]).toContain('vitest');
  });

  it('still runs vitest even when provisioning fails (fail-open)', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ok\n')
      .mockResolvedValueOnce('checked-out\n')
      .mockResolvedValueOnce('sha\n')
      .mockRejectedValueOnce(new Error('npm fail'))  // provisioning failure
      .mockResolvedValueOnce('1 passed\n');           // npx vitest run

    await runTrial(payloadFor({ test_refs: ['agent/src/handlers/foo.test.ts'] }));

    const calls = vi.mocked(execFileAsync).mock.calls;
    expect(calls[3][0]).toBe('npm');
    expect(calls[4][0]).toBe('npx'); // vitest still ran

    const report = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const result = JSON.parse(report.logs as string) as Record<string, unknown>;
    expect(result).toMatchObject({ ran: true, passed: true });
  });
});
