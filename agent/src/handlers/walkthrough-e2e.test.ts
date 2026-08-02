import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Hoisted mock handles for the candidate-stack controller ─────────────────
const { bootMock, teardownMock, ctorMock, deriveComposeProjectMock } = vi.hoisted(() => {
  const bootMock = vi.fn();
  const teardownMock = vi.fn().mockResolvedValue(undefined);
  const ctorMock = vi.fn().mockImplementation(function CandidateStackControllerMock() {
    return { boot: bootMock, teardown: teardownMock };
  });
  const deriveComposeProjectMock = vi.fn();
  return { bootMock, teardownMock, ctorMock, deriveComposeProjectMock };
});

// ── Module mocks ────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    apiPost: vi.fn().mockResolvedValue({ recorded: 1, skipped_not_passed: 0 }),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    readHandlerOutput: vi.fn(),
  };
});

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
});

vi.mock('./walkthrough-stack.js', () => ({
  CandidateStackController: ctorMock,
  deriveComposeProject: deriveComposeProjectMock,
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

vi.mock('fs', () => {
  const stub = {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    mkdirSync: vi.fn(),
  };
  return { ...stub, default: stub };
});

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// Preload the handlers barrel so audits.ts (source of resolveCandidateSha) binds
// against the mocked ../index.js in this module's context (mirrors specialized.test.ts).
import './index.js';
import {
  handleWalkthroughE2e,
  prepareRunnerIsolation,
  WALKTHROUGH_V26_RESULT_KEYS,
} from './walkthrough-e2e.js';
import { apiPost, persistAndPostStatus, readHandlerOutput, sendLog } from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { execFileSync } from 'child_process';
import { existsSync, readFileSync } from 'fs';

const TASK_ID = 'wt-task-1';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const CANDIDATE_SHA = 'a'.repeat(40);
const DEPLOYED_URL = 'http://deployed-orchestrator:9999';

const TASK = { project_metadata: { project_id: 'proj-1' } } as Record<string, unknown>;
const PAYLOAD = { base_branch: 'main', candidate_sha: CANDIDATE_SHA } as Record<string, unknown>;

const STACK = {
  backendUrl: 'http://127.0.0.1:18080',
  frontendUrl: 'http://localhost:13001',
  adminToken: 'local-candidate-admin-token',
  databaseUrl: 'postgresql+asyncpg://postgres:pw@127.0.0.1:15432/postgres',
  exercisedEngineSha: CANDIDATE_SHA,
};

const VALID_RESULT = {
  git_sha: CANDIDATE_SHA,
  run_timestamp: '2026-07-22T00:00:00Z',
  results: [
    { criterion_key: 'c-dfdc9d82', passed: true, notes: 'runner ok' },
    { criterion_key: 'c-f52fd975', passed: true, notes: 'tools ok' },
    { criterion_key: 'c-workday-session-lifecycle', passed: true, notes: 'workday ok' },
    { criterion_key: 'c-5214996f', passed: true, notes: 'meta ok' },
  ],
};

/** Find the execFileSync invocation that launched the walkthrough harness. */
function harnessCall() {
  return vi.mocked(execFileSync).mock.calls.find(
    (c) => Array.isArray(c[1]) && (c[1] as string[]).some((a) => a.includes('v26-live-walkthrough')),
  );
}

function evidencePosted(): boolean {
  return vi.mocked(apiPost).mock.calls.some((c) => c[0].includes('walkthrough-e2e-evidence'));
}

function lastStatus(): Record<string, unknown> {
  const calls = vi.mocked(persistAndPostStatus).mock.calls;
  return calls[calls.length - 1][1];
}

/** All four raw result rows the v26 harness emits. */
const ALL_CRITERIA_RESULT = {
  git_sha: CANDIDATE_SHA,
  run_timestamp: '2026-07-22T00:00:00Z',
  results: [
    { criterion_key: 'c-dfdc9d82', passed: true, notes: 'runner enrolled and heartbeat confirmed' },
    { criterion_key: 'c-f52fd975', passed: true, notes: 'tools lifecycle completed end-to-end' },
    { criterion_key: 'c-workday-session-lifecycle', passed: true, notes: 'workday session lifecycle complete' },
    { criterion_key: 'c-5214996f', passed: true, notes: 'all substantive walkthrough gates passed' },
  ],
};

describe('handleWalkthroughE2e — candidate-SHA stack targeting', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    teardownMock.mockResolvedValue(undefined);
    process.env.ORCHESTRATOR_URL = DEPLOYED_URL;
    process.env.ADMIN_API_KEY = 'agent-vm-admin-key';
  });

  it('drives the harness at the local candidate stack, NOT ORCHESTRATOR_URL, and posts exercised_engine_sha', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_RESULT);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const call = harnessCall();
    if (!call) throw new Error('walkthrough harness must be launched');
    const env = (call[2] as { env: Record<string, string> }).env;
    // The harness is pointed at the LOCAL candidate stack, never the deployed engine.
    expect(env.WALKTHROUGH_BACKEND_URL).toBe(STACK.backendUrl);
    expect(env.WALKTHROUGH_BACKEND_URL).not.toBe(DEPLOYED_URL);
    expect(env.WALKTHROUGH_FRONTEND_URL).toBe(STACK.frontendUrl);
    expect(env.WALKTHROUGH_ADMIN_TOKEN).toBe(STACK.adminToken);
    expect(env.WALKTHROUGH_PROJECT_ID).toBe('proj-1');
    expect(env.GIT_SHA).toBe(CANDIDATE_SHA);
    expect(env.WALKTHROUGH_RESULT_FILE).toContain(TASK_ID);

    // Evidence POST carries the SHA the candidate stack actually served.
    expect(evidencePosted()).toBe(true);
    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) => c[0].includes('walkthrough-e2e-evidence'));
    expect(evidenceCalls[0][0]).toContain('/projects/proj-1/');
    const body = evidenceCalls[0][1] as Record<string, unknown>;
    expect(body.candidate_sha).toBe(CANDIDATE_SHA);
    expect(body.exercised_engine_sha).toBe(CANDIDATE_SHA);

    // Exact-SHA targeting stays shallow: the base clone is depth 1 and the
    // pinned object is fetched explicitly at depth 1 before detached checkout.
    expect(prepareWorkspaceForRole).toHaveBeenCalledWith(
      expect.objectContaining({ depth: 1 }),
    );
    expect(execFileSync).toHaveBeenCalledWith(
      'git',
      ['fetch', '--depth', '1', 'origin', CANDIDATE_SHA],
      expect.objectContaining({ cwd: '/tmp/workspace' }),
    );
    expect(execFileSync).toHaveBeenCalledWith(
      'git',
      ['checkout', '--detach', CANDIDATE_SHA],
      expect.objectContaining({ cwd: '/tmp/workspace' }),
    );

    // COMPLETED with a durable candidate-SHA report, then torn down.
    const completed = lastStatus();
    expect(completed.status).toBe('COMPLETED');
    expect(completed.artifact_type).toBe('WALKTHROUGH_E2E_REPORT');
    expect(completed.logs).toBe(
      JSON.stringify({ ...VALID_RESULT, exercised_engine_sha: CANDIDATE_SHA }),
    );
    expect(teardownMock).toHaveBeenCalled();
  });

  it('fails closed on stack-boot failure: NO evidence posted, task FAILED, stack torn down', async () => {
    bootMock.mockRejectedValue(new Error('docker compose build failed'));

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    // No evidence ingestion POST whatsoever.
    expect(evidencePosted()).toBe(false);

    // The harness itself was never launched — the candidate stack never came up.
    expect(harnessCall()).toBeUndefined();

    // FAILED with the fail-closed category.
    const statusCall = lastStatus();
    expect(statusCall.status).toBe('FAILED');
    expect(statusCall.failure_category).toBe('candidate_stack_boot_failed');

    // Teardown still runs on the failure path.
    expect(teardownMock).toHaveBeenCalled();
  });
});

// ── Flow-to-criterion mapping ───────────────────────────────────────────────────

describe('handleWalkthroughE2e — flow-to-criterion mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    teardownMock.mockResolvedValue(undefined);
    vi.mocked(apiPost).mockResolvedValue({ recorded: 3, skipped_not_passed: 0 });
    process.env.ORCHESTRATOR_URL = DEPLOYED_URL;
    process.env.ADMIN_API_KEY = 'agent-vm-admin-key';
  });

  it('all four frozen raw criterion rows appear in the evidence POST body', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(ALL_CRITERIA_RESULT);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) =>
      c[0].includes('walkthrough-e2e-evidence'),
    );
    expect(evidenceCalls).toHaveLength(1);

    const body = evidenceCalls[0][1] as {
      results: Array<{ criterion_key: string; passed: boolean; notes: string | null }>;
    };
    const postedKeys = body.results.map((r) => r.criterion_key);
    expect(postedKeys).toEqual([...WALKTHROUGH_V26_RESULT_KEYS]);
  });

  it('absent notes field is normalised to null in the evidence body', async () => {
    bootMock.mockResolvedValue(STACK);
    const resultMixedNotes = {
      git_sha: CANDIDATE_SHA,
      run_timestamp: '2026-07-22T00:00:00Z',
      results: [
        { criterion_key: 'c-dfdc9d82', passed: true },                                // no notes
        { criterion_key: 'c-f52fd975', passed: false, notes: 'step 7e failed' },     // string notes
        { criterion_key: 'c-workday-session-lifecycle', passed: true, notes: 'ok' },
        { criterion_key: 'c-5214996f', passed: false, notes: 'substantive gate failed' },
      ],
    };
    vi.mocked(readHandlerOutput).mockReturnValue(resultMixedNotes);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) =>
      c[0].includes('walkthrough-e2e-evidence'),
    );
    const body = evidenceCalls[0][1] as {
      results: Array<{ criterion_key: string; passed: boolean; notes: string | null }>;
    };

    const dfdc = body.results.find((r) => r.criterion_key === 'c-dfdc9d82');
    expect(dfdc?.passed).toBe(true);
    expect(dfdc?.notes).toBeNull();

    const f52 = body.results.find((r) => r.criterion_key === 'c-f52fd975');
    expect(f52?.passed).toBe(false);
    expect(f52?.notes).toBe('step 7e failed');
  });

  it('qualification_attempt_id is forwarded to the evidence POST body when provided', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_RESULT);
    const payloadWithAttemptId = { ...PAYLOAD, qualification_attempt_id: 'qa-attempt-123' };

    await handleWalkthroughE2e(TASK_ID, payloadWithAttemptId, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) =>
      c[0].includes('walkthrough-e2e-evidence'),
    );
    const body = evidenceCalls[0][1] as Record<string, unknown>;
    expect(body.qualification_attempt_id).toBe('qa-attempt-123');
  });

  it('qualification_attempt_id is absent from evidence body when not in payload', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_RESULT);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) =>
      c[0].includes('walkthrough-e2e-evidence'),
    );
    const body = evidenceCalls[0][1] as Record<string, unknown>;
    expect(Object.prototype.hasOwnProperty.call(body, 'qualification_attempt_id')).toBe(false);
  });
});

// ── Error paths ────────────────────────────────────────────────────────────────

describe('handleWalkthroughE2e — error paths', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    teardownMock.mockResolvedValue(undefined);
    vi.mocked(apiPost).mockResolvedValue({ recorded: 1, skipped_not_passed: 0 });
    // Reset execFileSync to its module-level default so per-test overrides
    // from sibling tests (mockImplementation) do not bleed across.
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    process.env.ORCHESTRATOR_URL = DEPLOYED_URL;
    process.env.ADMIN_API_KEY = 'agent-vm-admin-key';
  });

  it.each([
    {
      field: 'project_id',
      payload: { candidate_sha: CANDIDATE_SHA },
      task: {},
    },
    {
      field: 'candidate_sha',
      payload: { project_id: 'proj-1' },
      task: {},
    },
  ])('rejects a dispatch missing required $field before cloning', async ({ field, payload, task }) => {
    await expect(
      handleWalkthroughE2e(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, { value: '' }),
    ).rejects.toThrow(field);

    expect(prepareWorkspaceForRole).not.toHaveBeenCalled();
  });

  it('classifies an exact-SHA checkout failure as clone_failed before booting a stack', async () => {
    vi.mocked(execFileSync).mockImplementation((cmd, args) => {
      if (cmd === 'git' && Array.isArray(args) && args.includes('checkout')) {
        throw new Error('fatal: reference is not a tree');
      }
      return Buffer.from('');
    });

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    expect(harnessCall()).toBeUndefined();
    expect(evidencePosted()).toBe(false);
    expect(bootMock).not.toHaveBeenCalled();
    expect(lastStatus()).toEqual(expect.objectContaining({
      status: 'FAILED',
      failure_category: 'clone_failed',
    }));
  });

  it('harness exits non-zero AND result file absent: FAILED with walkthrough_harness_failed', async () => {
    bootMock.mockResolvedValue(STACK);
    // Make only the walkthrough harness invocation throw; git commands succeed.
    vi.mocked(execFileSync).mockImplementation((_cmd, args) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (a.some((x) => typeof x === 'string' && x.includes('v26-live-walkthrough'))) {
        throw new Error('harness exit code 1');
      }
      return Buffer.from('');
    });
    // No result file written.
    vi.mocked(readHandlerOutput).mockReturnValue(undefined);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    expect(evidencePosted()).toBe(false);
    const status = lastStatus();
    expect(status.status).toBe('FAILED');
    expect(status.failure_category).toBe('walkthrough_harness_failed');
    expect(teardownMock).toHaveBeenCalled();
  });

  it('harness succeeds but result file is missing/unparseable: FAILED with walkthrough_result_unparseable', async () => {
    bootMock.mockResolvedValue(STACK);
    // Harness command returns successfully (default mock), but no result file.
    vi.mocked(readHandlerOutput).mockReturnValue(undefined);

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    expect(evidencePosted()).toBe(false);
    const status = lastStatus();
    expect(status.status).toBe('FAILED');
    expect(status.failure_category).toBe('walkthrough_result_unparseable');
    expect(teardownMock).toHaveBeenCalled();
  });

  it('rejects a parseable but truncated result subset before posting evidence', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue({
      ...VALID_RESULT,
      results: VALID_RESULT.results.slice(0, 3),
    });

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    expect(evidencePosted()).toBe(false);
    expect(lastStatus().status).toBe('FAILED');
    expect(lastStatus().failure_category).toBe('walkthrough_result_unparseable');
    expect(teardownMock).toHaveBeenCalled();
  });

  it('rejects duplicate or non-boolean raw rows before posting evidence', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue({
      ...VALID_RESULT,
      results: [
        ...VALID_RESULT.results.slice(0, 3),
        { criterion_key: 'c-workday-session-lifecycle', passed: 'true' },
      ],
    });

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    expect(evidencePosted()).toBe(false);
    expect(lastStatus().status).toBe('FAILED');
    expect(lastStatus().failure_category).toBe('walkthrough_result_unparseable');
  });

  it('evidence POST throws: task still COMPLETED (evidence failure is non-fatal)', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(VALID_RESULT);
    vi.mocked(apiPost).mockRejectedValue(new Error('network error posting evidence'));

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    const status = lastStatus();
    expect(status.status).toBe('COMPLETED');
    expect(teardownMock).toHaveBeenCalled();
  });

  it('stack teardown is always called even when evidence POST and result parsing both fail', async () => {
    bootMock.mockResolvedValue(STACK);
    vi.mocked(readHandlerOutput).mockReturnValue(undefined);
    vi.mocked(apiPost).mockRejectedValue(new Error('should never be called'));

    await handleWalkthroughE2e(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, { value: '' });

    // Teardown guaranteed even on the no-result failure path.
    expect(teardownMock).toHaveBeenCalled();
  });
});

// ── prepareRunnerIsolation (Layer 1 — best-effort, never fatal) ──────────────────

describe('prepareRunnerIsolation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset the subprocess/fs mocks to their permissive module-level defaults so
    // per-test overrides do not bleed across.
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(existsSync).mockReturnValue(false);
    vi.mocked(readFileSync).mockReturnValue('');
  });

  it('never throws when every probe and install fails', async () => {
    // Both the subprocess probes AND the sysctl read blow up.
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('command not found');
    });
    vi.mocked(readFileSync).mockImplementation(() => {
      throw new Error('no such sysctl');
    });

    // The promise must resolve — an isolation failure must not abort the caller.
    const result = await prepareRunnerIsolation(null);
    expect(result).toBeDefined();
    expect(result.isolationOk).toBe(false);
    expect(typeof result.detail).toBe('string');
  });

  it('returns isolationOk:false with a descriptive detail when bwrap/userns/agent are unavailable', async () => {
    // No bwrap, no userns, no docker, no agent entry point.
    vi.mocked(execFileSync).mockImplementation(() => {
      throw new Error('unavailable');
    });
    vi.mocked(existsSync).mockReturnValue(false);
    vi.mocked(readFileSync).mockReturnValue('0'); // userns explicitly disabled

    const { isolationOk, detail } = await prepareRunnerIsolation(null);
    expect(isolationOk).toBe(false);
    expect(detail).toContain('bwrap:false');
    expect(detail).toContain('docker:false');
    expect(detail).toContain('agent:false');
    expect(detail).toContain('userns:false');
  });

  it('returns isolationOk:true via the bwrap path when the probes report capable', async () => {
    // bwrap + userns + agent entry present, but NO usable docker daemon —
    // proves the primary rootless-bwrap composition, not the docker shortcut.
    vi.mocked(execFileSync).mockImplementation((cmd) => {
      if (cmd === 'docker') throw new Error('docker daemon not running');
      return Buffer.from('');
    });
    vi.mocked(readFileSync).mockReturnValue('1'); // unprivileged_userns_clone = 1
    vi.mocked(existsSync).mockReturnValue(true);   // agent dist/index.js present

    const { isolationOk, detail } = await prepareRunnerIsolation(null);
    expect(isolationOk).toBe(true);
    expect(detail).toContain('bwrap:true');
    expect(detail).toContain('userns:true');
    expect(detail).toContain('agent:true');
    expect(detail).toContain('docker:false');
  });

  it('logs a compact bwrap/docker/agent isolation summary to the stream', async () => {
    await prepareRunnerIsolation(null);
    const summaryLogged = vi.mocked(sendLog).mock.calls.some(
      (c) => typeof c[1] === 'string'
        && c[1].includes('bwrap:')
        && c[1].includes('docker:')
        && c[1].includes('agent:'),
    );
    expect(summaryLogged).toBe(true);
  });
});
