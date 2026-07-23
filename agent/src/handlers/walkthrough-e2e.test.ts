import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Hoisted mock handles for the candidate-stack controller ─────────────────
const { bootMock, teardownMock, ctorMock, deriveComposeProjectMock } = vi.hoisted(() => {
  const bootMock = vi.fn();
  const teardownMock = vi.fn().mockResolvedValue(undefined);
  const ctorMock = vi.fn().mockImplementation(() => ({ boot: bootMock, teardown: teardownMock }));
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
import { handleWalkthroughE2e } from './walkthrough-e2e.js';
import { apiPost, persistAndPostStatus, readHandlerOutput } from '../index.js';
import { execFileSync } from 'child_process';

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
  results: [{ criterion_key: 'c-dfdc9d82', passed: true, notes: 'ok' }],
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

    // Evidence POST carries the SHA the candidate stack actually served.
    expect(evidencePosted()).toBe(true);
    const evidenceCalls = vi.mocked(apiPost).mock.calls.filter((c) => c[0].includes('walkthrough-e2e-evidence'));
    const body = evidenceCalls[0][1] as Record<string, unknown>;
    expect(body.candidate_sha).toBe(CANDIDATE_SHA);
    expect(body.exercised_engine_sha).toBe(CANDIDATE_SHA);

    // COMPLETED and torn down.
    expect(lastStatus().status).toBe('COMPLETED');
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
