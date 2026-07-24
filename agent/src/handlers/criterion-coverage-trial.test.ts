import { beforeEach, describe, it, expect, vi } from 'vitest';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/criterion-coverage-workspace',
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

import { handleCriterionCoverageTrial } from './criterion-coverage-trial.js';
import { execFileAsync, persistAndPostStatus } from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';

const statusRef = { value: '' };

function lastReportedResult(): Record<string, unknown> {
  const call = vi.mocked(persistAndPostStatus).mock.calls[0];
  return JSON.parse(call[1].logs as string) as Record<string, unknown>;
}

describe('handleCriterionCoverageTrial — green Python run', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('reports ran=true passed=true on a green Python test run', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('executed-sha\n')   // git rev-parse HEAD
      .mockResolvedValueOnce('3 passed\n');        // test runner

    await handleCriterionCoverageTrial(
      'task-green',
      {
        criterion_ids: ['crit-abc', 'crit-def'],
        source_task_id: 'src-task-1',
        head_sha: 'queued-sha',
        test_refs: ['backend/testing/test_acceptance_criteria.py'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const result = lastReportedResult();
    expect(result).toMatchObject({
      type: 'criterion_coverage_trial',
      ran: true,
      passed: true,
      could_not_run: false,
      criterion_ids: ['crit-abc', 'crit-def'],
      source_task_id: 'src-task-1',
    });
  });

  it('propagates criterion_ids and source_task_id into the result', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('sha-xyz\n')
      .mockResolvedValueOnce('1 passed\n');

    await handleCriterionCoverageTrial(
      'task-ids',
      {
        criterion_ids: ['crit-1', 'crit-2', 'crit-3'],
        source_task_id: 'upstream-task-abc',
        head_sha: 'sha-xyz',
        test_refs: ['backend/testing/test_foo.py'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const result = lastReportedResult();
    expect(result.criterion_ids).toEqual(['crit-1', 'crit-2', 'crit-3']);
    expect(result.source_task_id).toBe('upstream-task-abc');
    expect(result.type).toBe('criterion_coverage_trial');
  });
});

describe('handleCriterionCoverageTrial — could_not_run paths', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('reports could_not_run=true for an empty test_refs list (no clone attempted)', async () => {
    await handleCriterionCoverageTrial(
      'task-no-refs',
      {
        criterion_ids: ['crit-abc'],
        source_task_id: 'src-1',
        head_sha: 'sha-1',
        test_refs: [],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
    const result = lastReportedResult();
    expect(result).toMatchObject({ ran: false, passed: false, could_not_run: true });
  });

  it('reports could_not_run=true for a mixed-language ref set (untargetable)', async () => {
    await handleCriterionCoverageTrial(
      'task-mixed',
      {
        criterion_ids: ['crit-abc'],
        source_task_id: 'src-1',
        head_sha: 'sha-1',
        test_refs: ['backend/testing/test_foo.py', 'agent/src/handlers/foo.test.ts'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
    const result = lastReportedResult();
    expect(result).toMatchObject({ ran: false, passed: false, could_not_run: true });
  });

  it('reports could_not_run=true for an unknown file extension', async () => {
    await handleCriterionCoverageTrial(
      'task-unknown-ext',
      {
        criterion_ids: ['crit-abc'],
        source_task_id: 'src-2',
        head_sha: 'sha-2',
        test_refs: ['docs/something.md'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
    const result = lastReportedResult();
    expect(result.could_not_run).toBe(true);
  });
});

describe('handleCriterionCoverageTrial — checkout provenance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('reports the executed checkout SHA (head_sha) distinct from the queued SHA (queued_head_sha)', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('actual-checkout-sha\n')  // git rev-parse HEAD
      .mockResolvedValueOnce('2 passed\n');              // test run

    await handleCriterionCoverageTrial(
      'task-sha',
      {
        criterion_ids: ['crit-1'],
        source_task_id: 'src-task',
        head_sha: 'originally-queued-sha',
        test_refs: ['backend/testing/test_acceptance_criteria.py'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const result = lastReportedResult();
    expect(result.head_sha).toBe('actual-checkout-sha');
    expect(result.queued_head_sha).toBe('originally-queued-sha');
    // The two SHAs are deliberately different — this is the drift-visibility invariant.
    expect(result.head_sha).not.toBe(result.queued_head_sha);
  });

  it('reports head_sha=null when rev-parse fails, without affecting ran/passed', async () => {
    vi.mocked(execFileAsync)
      .mockRejectedValueOnce(new Error('git unavailable'))  // rev-parse fails
      .mockResolvedValueOnce('1 passed\n');                  // test run still proceeds

    await handleCriterionCoverageTrial(
      'task-sha-fail',
      {
        criterion_ids: ['crit-1'],
        source_task_id: 'src-task',
        head_sha: 'queued-sha',
        test_refs: ['backend/testing/test_acceptance_criteria.py'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const result = lastReportedResult();
    expect(result.head_sha).toBeNull();
    expect(result.queued_head_sha).toBe('queued-sha');
    expect(result.ran).toBe(true);
    expect(result.passed).toBe(true);
    expect(result.could_not_run).toBe(false);
  });
});

describe('handleCriterionCoverageTrial — TS/JS refs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prepareWorkspaceForRole).mockResolvedValue(undefined);
  });

  it('runs vitest for TS refs and reports ran+passed on a green run', async () => {
    vi.mocked(execFileAsync)
      .mockResolvedValueOnce('ts-sha\n')  // git rev-parse HEAD
      .mockResolvedValueOnce('1 passed\n'); // npx vitest run (or npm install + vitest)

    await handleCriterionCoverageTrial(
      'task-ts',
      {
        criterion_ids: ['crit-ts'],
        source_task_id: 'src-ts',
        head_sha: 'ts-sha',
        test_refs: ['agent/src/handlers/criterion-coverage-trial.test.ts'],
      },
      {}, 'token', 'owner', 'repo', null, statusRef,
    );

    const calls = vi.mocked(execFileAsync).mock.calls;
    expect(calls[0][0]).toBe('git');  // rev-parse HEAD

    // The last call must be npx vitest run (test runner)
    const lastCall = calls[calls.length - 1];
    expect(lastCall[0]).toBe('npx');
    expect(lastCall[1]).toContain('vitest');

    const result = lastReportedResult();
    expect(result).toMatchObject({ ran: true, passed: true, could_not_run: false });
  });
});
