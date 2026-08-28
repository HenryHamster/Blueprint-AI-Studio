import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
  // Keep module-level provider/auth defaults deterministic across local and CI
  // environments. Some test cases call helpers without explicit provider args.
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  // Clear before index.ts loads so the module-level LLM_CLI_TRANSLATION
  // constant is null. Tests that verify translation behavior set the env var
  // themselves and call parseLLMCliTranslation() directly.
  delete process.env.LLM_CLI_TRANSLATION;
  return { mockReadFileSync };
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('fs', () => ({
  default: {
    readFileSync: mockReadFileSync,
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    openSync: vi.fn().mockReturnValue(3),
    closeSync: vi.fn(),
    chmodSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  spawn: vi.fn().mockReturnValue({
    stdin: { on: vi.fn(), end: vi.fn() },
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execSync: vi.fn().mockReturnValue(''),
  // reset.ts (now imported transitively via index.ts) wraps execFile with
  // promisify; supply the (cmd, args, opts, callback) shape so reset's
  // own dependency surface doesn't break unrelated index tests.
  execFile: (
    _cmd: string,
    _args: string[],
    _opts: unknown,
    cb: (err: Error | null, stdout: string, stderr: string) => void,
  ) => {
    void Promise.resolve().then(() => { cb(null, '', ''); });
  },
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));


// ── Imports (after mocks are registered) ─────────────────────────────────────
import {
  missingVmProviderConfigFiles,
  recordToolUse,
  snapshotToolTrace,
  clearToolTrace,
  appendToLogBuffer,
  snapshotLogBuffer,
  clearLogBuffer,
  replayLogBufferRateLimited,
  getProviderRunner,
  persistAndPostStatus,

  _consumePendingParkDirectiveForTesting,
  continueWarmContinuation,
  pollForNextRole,
  bootstrapPoolMode,
  refreshRuntimeState,
  _resetTelemetryForTesting,
  _setRoleClaimTimestampForTesting,
  _getFirstToolUseAtForTesting,
  _setWarmStartForTesting,
  _setHandlerDurationForTesting,
  _setAgentRuntimeFingerprintForTesting,
  _stopHeartbeatForTesting,
  getAgentToken,
  resetPerRoleModuleState,
  performWarmRetarget,
  __setWarmRetargetDepsForTesting,
  MATERIALIZE_SCRIPT_PATH,
  _setAgentTokenForTesting,
  _resetCredentialStateForTesting,
  _captureBootCredentialSnapshotForTesting,
  _captureBootProviderConfigPresenceForTesting,
  _materializeClaimCredentialStateForTesting,
  _getActiveCredentialStateSourceForTesting,
  cleanupWorkspaceOnProcessExit,
  main,
  WORKSPACE,
  type SecretManifest,
  type FailureCategory,
} from './index.js';
import {
  __setCommandExistsOverrideForSmoke,
  __resetClaudeCapabilityCacheForTesting,
} from './providers/runners.js';
import { prepareWorkspaceForRole, resetWorkspacePrepOutcome } from './workspacePrep.js';
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';

describe('process-exit workspace ownership', () => {
  it('preserves a local-runner checkout for same-task Resident recovery', () => {
    const remove = vi.fn();

    expect(cleanupWorkspaceOnProcessExit(
      '/workspace/agent',
      'runner:707de5fb-7b6e-406a-9105-774b28ea23be',
      remove,
    )).toBe(false);
    expect(remove).not.toHaveBeenCalled();
  });

  it('retains process-owned cleanup for non-runner agents', () => {
    const remove = vi.fn();

    expect(cleanupWorkspaceOnProcessExit(
      '/workspace/agent',
      'agent-vm-cloud-1',
      remove,
    )).toBe(true);
    expect(remove).toHaveBeenCalledWith(
      'rm',
      ['-rf', '/workspace/agent'],
      { encoding: 'utf-8', timeout: 30_000 },
    );
  });
});

/** fetch-shaped vi.fn: Promise-returning implementations satisfy
 *  @typescript-eslint/no-misused-promises (a bare vi.fn() infers a
 *  void-returning procedure, which every `.mockImplementation(() => Promise…)`
 *  call then violates). */
const makeFetchMock = () => vi.fn<(input: unknown, init?: RequestInit) => Promise<unknown>>();
type FetchMock = ReturnType<typeof makeFetchMock>;

const initialPoolPriorTaskId = '00000000-0000-0000-0000-000000000000';

function mockJwtWithVmInstanceId(vmInstanceId: string): string {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ sub: 'test-task', vm_instance_id: vmInstanceId })).toString('base64url');
  return `${header}.${payload}.fakesig`;
}


beforeEach(() => {
  // Bypass the pre-flight CLI-exists check: provider binaries (claude, codex, etc.)
  // are not installed in the test environment; spawn is mocked separately.
  __setCommandExistsOverrideForSmoke(() => true);
  __resetClaudeCapabilityCacheForTesting();
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  delete process.env.LLM_MODEL;
  delete process.env.LLM_SETTINGS;
  delete process.env.LLM_CLI_TRANSLATION;
  delete process.env.LLM_EXECUTION_MODE;
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  delete process.env.CLAUDE_CODE_EFFORT_LEVEL;
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.ANTHROPIC_AUTH_TOKEN;
  delete process.env.ANTHROPIC_BASE_URL;
  delete process.env.ANTHROPIC_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_OPUS_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_SONNET_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  delete process.env.CLAUDE_CODE_SUBAGENT_MODEL;
  delete process.env.ZAI_API_KEY;
  delete process.env.ZAI_ANTHROPIC_BASE_URL;
  delete process.env.ZAI_BASE_URL;
  delete process.env.DEEPSEEK_API_KEY;
  delete process.env.DEEPSEEK_ANTHROPIC_BASE_URL;
  delete process.env.DEEPSEEK_BASE_URL;
  delete process.env.OPENAI_API_KEY;
  delete process.env.CODEX_API_KEY;
  delete process.env.CODEX_HOME;
  delete process.env.GEMINI_API_KEY;
  delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
  delete process.env.GOOGLE_CLOUD_PROJECT;
  delete process.env.GOOGLE_CLOUD_LOCATION;
  _resetCredentialStateForTesting();
  refreshRuntimeState();
});


// ── recordToolUse ────────────────────────────────────────────────────────────
describe('recordToolUse', () => {
  it('records a tool use entry with timestamp, tool name, and input preview', () => {
    recordToolUse('Bash', { command: 'ls' });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.tool).toBe('Bash');
    expect(entry.input_preview).toBe('{"command":"ls"}');
    expect(entry.t).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  it('truncates input_preview to 200 characters for long inputs', () => {
    recordToolUse('Bash', { command: 'x'.repeat(500) });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview.length).toBe(200);
  });

  it("records '<unserializable>' for inputs with circular references", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    recordToolUse('Bash', circular);
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('<unserializable>');
  });

  it('evicts oldest entry when buffer exceeds 20 entries (TOOL_TRACE_MAX)', () => {
    for (let i = 0; i < 25; i++) {
      recordToolUse(`tool-${i}`, { n: i });
    }
    const trace = snapshotToolTrace();
    expect(trace.length).toBe(20);
    const tools = trace.map(e => e.tool);
    expect(tools).toContain('tool-24');
    expect(tools).toContain('tool-5');
    expect(tools).not.toContain('tool-4');
    expect(tools).not.toContain('tool-0');
  });

  it('handles string input directly without JSON.stringify', () => {
    recordToolUse('Write', 'raw string value');
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).toBe('raw string value');
  });

  it('sanitizes credential patterns in input_preview', () => {
    recordToolUse('Bash', { command: 'curl -H "Authorization: token ghp_abc123secretXYZ" https://api.github.com' });
    const trace = snapshotToolTrace();
    const entry = trace[trace.length - 1];
    expect(entry.input_preview).not.toContain('ghp_abc123secretXYZ');
    expect(entry.input_preview).toContain('ghp_***');
  });

  it('can refresh the latest entry timestamp without adding a new tool use', () => {
    clearToolTrace();
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'));
      recordToolUse('Bash', { command: './pr open' });
      const initial = snapshotToolTrace();

      vi.setSystemTime(new Date('2026-01-01T00:09:00.000Z'));
      const refreshed = snapshotToolTrace({ refreshLastEntry: true });

      expect(refreshed.length).toBe(1);
      expect(refreshed[0].tool).toBe('Bash');
      expect(refreshed[0].input_preview).toBe('{"command":"./pr open"}');
      expect(refreshed[0].t).toBe('2026-01-01T00:09:00.000Z');
      expect(refreshed[0].t).not.toBe(initial[0].t);
    } finally {
      vi.useRealTimers();
      clearToolTrace();
    }
  });
});

// ── replayLogBufferRateLimited ────────────────────────────────────────────────
describe('replayLogBufferRateLimited', () => {
  beforeEach(() => {
    clearLogBuffer();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    clearLogBuffer();
  });

  it('sends all buffered entries and empties the buffer for a small batch', async () => {
    appendToLogBuffer('msg-1');
    appendToLogBuffer('msg-2');
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    await vi.runAllTimersAsync();
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(2);
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('does not exceed 15 sends before the 1-second throttle window elapses', async () => {
    for (let i = 0; i < 16; i++) appendToLogBuffer(`msg-${i}`);
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    // After minimal time: first batch of 15 sent, the 16th is still waiting.
    await vi.advanceTimersByTimeAsync(50);
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(15);
    expect(snapshotLogBuffer().length).toBe(1);
    // Advance past the throttle window so the second batch fires.
    await vi.advanceTimersByTimeAsync(1000);
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(16);
    expect(snapshotLogBuffer().length).toBe(0);
  });

  it('drains a full 1000-entry buffer fully across multiple batches', async () => {
    for (let i = 0; i < 1000; i++) appendToLogBuffer(`msg-${i}`);
    expect(snapshotLogBuffer().length).toBe(1000);
    const ws = { send: vi.fn() } as unknown as WebSocket;
    const replay = replayLogBufferRateLimited(ws);
    await vi.advanceTimersByTimeAsync(100_000);
    await replay;
    expect((ws.send as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1000);
    expect(snapshotLogBuffer().length).toBe(0);
  });
});

// ── Phase 0 telemetry — setup_secs / warm_start / prior_role_vm_id ───────────
describe('persistAndPostStatus telemetry enrichment', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
    fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    _resetTelemetryForTesting();
    _setRoleClaimTimestampForTesting(null);
    _setHandlerDurationForTesting(null);
  });

  it('computes setup_secs from claim → first tool use gap', async () => {
    const claimAt = 1_000_000;
    _setRoleClaimTimestampForTesting(claimAt);
    // Simulate the SDK message loop calling recordToolUse 12.5s after claim.
    const dateNowSpy = vi.spyOn(Date, 'now').mockReturnValue(claimAt + 12_500);
    try {
      recordToolUse('Read', { file_path: '/x' });
      expect(_getFirstToolUseAtForTesting()).toBe(claimAt + 12_500);
    } finally {
      dateNowSpy.mockRestore();
    }

    await persistAndPostStatus('task-X', { task_id: 'task-X', status: 'COMPLETED' });
    expect(fetchMock).toHaveBeenCalled();
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number; warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.setup_secs).toBe(12.5);
  });

  it('warm_start defaults to false on initial role; null prior_role_vm_id', async () => {
    _setRoleClaimTimestampForTesting(2_000_000);
    await persistAndPostStatus('task-cold', { task_id: 'task-cold', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
    expect(body.warm_start).toBe(false);
    expect(body.prior_role_vm_id).toBeNull();
  });

  it('warm_start=true emits prior_role_vm_id from VM_INSTANCE_ID', async () => {
    const orig = process.env.VM_INSTANCE_ID;
    process.env.VM_INSTANCE_ID = 'agent-vm-warm-7';
    try {
      _setRoleClaimTimestampForTesting(3_000_000);
      _setWarmStartForTesting(true);
      await persistAndPostStatus('task-warm', { task_id: 'task-warm', status: 'COMPLETED' });
      const body = JSON.parse(
        (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
      ) as { warm_start?: boolean; prior_role_vm_id?: string | null };
      expect(body.warm_start).toBe(true);
      expect(body.prior_role_vm_id).toBe('agent-vm-warm-7');
    } finally {
      if (orig === undefined) delete process.env.VM_INSTANCE_ID;
      else process.env.VM_INSTANCE_ID = orig;
    }
  });

  it('omits setup_secs when no tool use was recorded', async () => {
    _setRoleClaimTimestampForTesting(4_000_000);
    // No recordToolUse call — firstToolUseAt stays null.
    await persistAndPostStatus('task-quiet', { task_id: 'task-quiet', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { setup_secs?: number };
    expect(body.setup_secs).toBeUndefined();
  });

  it('enrichStatusPayload includes handler_duration_secs after the simulated handler-complete capture', async () => {
    // Mirrors what runRoleOnce does on the success path: capture
    // (Date.now() - roleClaimTimestamp)/1000 after the handler returns.
    // We seed the mutable directly via the test helper instead of running
    // the full role-loop.
    _setHandlerDurationForTesting(87.3);
    await persistAndPostStatus('task-h', { task_id: 'task-h', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBe(87.3);
  });

  it('omits handler_duration_secs when null (e.g. failure paths or pre-handler statuses)', async () => {
    // Default state — _setHandlerDurationForTesting(null) in beforeEach.
    await persistAndPostStatus('task-q', { task_id: 'task-q', status: 'COMPLETED' });
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as { body?: string }).body ?? '{}',
    ) as { handler_duration_secs?: number };
    expect(body.handler_duration_secs).toBeUndefined();
  });
});

describe('persistAndPostStatus workspace_prep shipping', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    resetWorkspacePrepOutcome();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
    fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetWorkspacePrepOutcome();
    delete process.env.BLUEPRINT_WARM_HANDOFF;
    delete process.env.BLUEPRINT_LANE_MODE;
  });

  it('includes the recorded workspace prep outcome in /api/agent/status', async () => {
    const existsSpy = vi.mocked(fs.existsSync);
    existsSpy.mockImplementation(
      value => String(value).replaceAll('\\', '/') === '/ws/.git',
    );
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    process.env.BLUEPRINT_LANE_MODE = '1';

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/warm',
    });

    await persistAndPostStatus('task-wp', { task_id: 'task-wp', status: 'COMPLETED' });

    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)?.[1] as { body?: string }).body ?? '{}',
    ) as {
      workspace_prep?: {
        path?: string;
        warm_reuse_succeeded?: boolean;
        checkpoint_reset_applied?: boolean;
        lane_phase?: boolean;
      };
    };
    expect(body.workspace_prep).toMatchObject({
      path: 'warm',
      warm_reuse_succeeded: true,
      checkpoint_reset_applied: false,
      lane_phase: true,
    });
  });

  it('resetPerRoleModuleState clears stale workspace_prep before the next role ships', async () => {
    const existsSpy = vi.mocked(fs.existsSync);
    existsSpy.mockImplementation(
      value => String(value).replaceAll('\\', '/') === '/ws/.git',
    );
    process.env.BLUEPRINT_WARM_HANDOFF = '1';
    process.env.BLUEPRINT_LANE_MODE = '1';

    await prepareWorkspaceForRole({
      workspace: '/ws',
      repoUrl: 'https://x-access-token:tok@github.com/o/r.git',
      githubToken: 'tok',
      branch: 'feature/warm',
    });
    resetPerRoleModuleState({ warmStart: false });

    await persistAndPostStatus('task-wp-reset', {
      task_id: 'task-wp-reset',
      status: 'COMPLETED',
    });

    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)?.[1] as { body?: string }).body ?? '{}',
    ) as { workspace_prep?: unknown };
    expect(body.workspace_prep).toBeUndefined();
  });
});

describe('park directives and warm continuation', () => {
  const originalTaskId = process.env.TASK_ID;
  const originalVmInstanceId = process.env.VM_INSTANCE_ID;
  const originalAgentToken = process.env.AGENT_SECRET_TOKEN;
  const originalLiveAgentToken = getAgentToken();

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalVmInstanceId !== undefined) process.env.VM_INSTANCE_ID = originalVmInstanceId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalAgentToken !== undefined) process.env.AGENT_SECRET_TOKEN = originalAgentToken;
    else delete process.env.AGENT_SECRET_TOKEN;
    _setAgentTokenForTesting(originalLiveAgentToken);
    _consumePendingParkDirectiveForTesting('task-park');
  });

  it('records a park directive from /api/agent/status', async () => {
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        action: 'park',
        retry_after_until: '2030-01-01T00:00:10Z',
        park_deadline: '2030-01-01T00:00:30Z',
      }),
      text: () => Promise.resolve(''),
      headers: { get: () => null },
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await persistAndPostStatus('task-park', {
      task_id: 'task-park',
      status: 'FAILED',
      failure_category: 'rate_limited',
    });

    expect(result).toMatchObject({
      action: 'park',
      retry_after_until: '2030-01-01T00:00:10Z',
      park_deadline: '2030-01-01T00:00:30Z',
    });
    expect(_consumePendingParkDirectiveForTesting('task-park')).toEqual({
      action: 'park',
      retryAfterUntil: '2030-01-01T00:00:10Z',
      parkDeadline: '2030-01-01T00:00:30Z',
    });
  });

  it('uses the park deadline as the poll timeout and exits cleanly when it expires', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
    process.env.TASK_ID = 'task-park';
    process.env.VM_INSTANCE_ID = 'vm-park-1';

    let observedTimeoutMs: number | undefined;
    const result = await continueWarmContinuation({
      priorTaskId: 'task-park',
      priorPrNumber: 17,
      baseBranch: 'main',
      parkDirective: {
        action: 'park',
        retryAfterUntil: '2030-01-01T00:00:05Z',
        parkDeadline: '2030-01-01T00:00:15Z',
      },
    }, {
      pollForNextRole: opts => {
        observedTimeoutMs = opts.timeoutMs;
        return Promise.resolve({ action: 'exit', reason: 'poll_timeout' });
      },
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: allowJwtFallback => (allowJwtFallback ? 'vm-park-1' : process.env.VM_INSTANCE_ID || null),
    });

    expect(result).toEqual({ action: 'exit' });
    expect(observedTimeoutMs).toBe(15_000);
    expect(process.env.TASK_ID).toBe('');
  });

  it('polls and cold-starts the next repository role when the warm workspace is invalid', async () => {
    const pollForNextRole = vi.fn().mockResolvedValue({
      action: 'claim',
      taskId: 'cold-start-task',
      agentToken: 'rotated-cold-start-jwt',
      workspaceRequirement: 'repo',
    });
    const resetForNextRole = vi.fn();
    const activateClaimedRole = vi.fn();
    const materializeClaimCredentialState = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'clone-failed-task',
      priorPrNumber: null,
      baseBranch: 'main',
    }, {
      pollForNextRole,
      resetForNextRole,
      activateClaimedRole,
      materializeClaimCredentialState,
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-clone-failed',
      isWorkspaceValid: () => false,
    });

    expect(result).toEqual({ action: 'continue' });
    expect(pollForNextRole).toHaveBeenCalledOnce();
    expect(activateClaimedRole).toHaveBeenCalledWith(
      expect.objectContaining({ taskId: 'cold-start-task' }),
      'reset_failed: warm-claim',
    );
    expect(resetForNextRole).not.toHaveBeenCalled();
    expect(materializeClaimCredentialState).toHaveBeenCalledOnce();
  });

  it('continues into repository preparation when warm reset fails after claim', async () => {
    const resetForNextRole = vi.fn().mockRejectedValue(new Error('corrupt git metadata'));
    const materializeClaimCredentialState = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'prior-task',
      priorPrNumber: null,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'claimed-before-reset',
        agentToken: 'rotated-reset-jwt',
        workspaceRequirement: 'repo',
      }),
      resetForNextRole,
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState,
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-reset-fallback',
      isWorkspaceValid: () => true,
    });

    expect(result).toEqual({ action: 'continue' });
    expect(resetForNextRole).toHaveBeenCalledOnce();
    expect(materializeClaimCredentialState).toHaveBeenCalledOnce();
  });

  it('falls back to the task JWT vm id and re-targets after reclaim', async () => {
    process.env.TASK_ID = 'task-park';
    delete process.env.VM_INSTANCE_ID;
    _setAgentTokenForTesting(mockJwtWithVmInstanceId('vm-from-jwt'));

    const callOrder: string[] = [];
    const resetForNextRole = vi.fn().mockImplementation(() => {
      callOrder.push('reset');
      return Promise.resolve();
    });
    const activateClaimedRole = vi.fn().mockImplementation((claim: { taskId?: string; agentToken?: string }) => {
      callOrder.push('activate');
      if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
      if (claim.taskId) process.env.TASK_ID = claim.taskId;
    });
    const materializeClaimCredentialState = vi.fn().mockImplementation(() => {
      callOrder.push('materialize');
    });
    const performWarmRetarget = vi.fn().mockImplementation((_manifest: unknown, opts: { taskId?: string }) => {
      callOrder.push(`retarget:${opts.taskId}`);
      return Promise.resolve();
    });

    const result = await continueWarmContinuation({
      priorTaskId: 'task-park',
      priorPrNumber: 9,
      baseBranch: 'main',
      parkDirective: {
        action: 'park',
        retryAfterUntil: '2030-01-01T00:00:05Z',
        parkDeadline: '2030-01-01T00:00:15Z',
      },
    }, {
      pollForNextRole: opts => {
        callOrder.push(`poll:${opts.vmInstanceId}`);
        return Promise.resolve({
          action: 'claim',
          taskId: 'task-next',
          agentToken: 'rotated-jwt',
          secretManifest: {
            env: { PROVIDER: 'anthropic' },
            files: [{ path: '/tmp/provider.json', content: '{}', mode: '0600' }],
          },
        });
      },
      resetForNextRole,
      activateClaimedRole,
      materializeClaimCredentialState,
      performWarmRetarget,
      resolveVmInstanceId: allowJwtFallback => (allowJwtFallback ? 'vm-from-jwt' : null),
    });

    expect(result).toEqual({ action: 'exit' });
    expect(callOrder).toEqual([
      'poll:vm-from-jwt',
      'activate',
      'reset',
      'materialize',
      'retarget:task-next',
    ]);
    expect(performWarmRetarget).toHaveBeenCalledWith(
      expect.objectContaining({ env: { PROVIDER: 'anthropic' } }),
      { rotatedJwt: 'rotated-jwt', taskId: 'task-next' },
    );
    expect(process.env.TASK_ID).toBe('task-next');
  });

  it('refreshes a same-account rotating credential without re-exec', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-codex-refresh';
    const materializeClaimCredentialState = vi.fn();
    const performWarmRetarget = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 10,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-codex-next',
        agentToken: 'rotated-codex-jwt',
        secretManifest: {
          env: { PROVIDER: 'openai', AUTH_MODE: 'oauth' },
          files: [{ path: '/opt/agent/openai/auth.json', content: '{}' }],
          metadata: { retarget: true, credential_refresh_only: true },
        },
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState,
      performWarmRetarget,
      resolveVmInstanceId: () => 'vm-codex-refresh',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(materializeClaimCredentialState).toHaveBeenCalledOnce();
    expect(performWarmRetarget).not.toHaveBeenCalled();
  });

  it('does not reset or reuse the repository for a discovery-review warm claim', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-memory-worker';
    const resetForNextRole = vi.fn();
    const materializeClaimCredentialState = vi.fn();

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 10,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'discovery-review-task',
        agentToken: 'rotated-discovery-jwt',
        workspaceRequirement: 'none',
      }),
      resetForNextRole,
      activateClaimedRole: vi.fn(),
      materializeClaimCredentialState,
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-memory-worker',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(resetForNextRole).not.toHaveBeenCalled();
    expect(materializeClaimCredentialState).toHaveBeenCalledOnce();
  });

  it('sets lane workspace env for a lane-phase warm claim', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-lane-claim';
    delete process.env.BLUEPRINT_LANE_MODE;
    delete process.env.BLUEPRINT_LANE_CHECKPOINT_SHA;

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 23,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-lane-next',
        agentToken: 'rotated-lane-jwt',
        isLanePhase: true,
        laneCheckpointSha: 'abc1234def5678901234567890123456789012ab',
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn((claim: { taskId?: string; agentToken?: string }) => {
        if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
        if (claim.taskId) process.env.TASK_ID = claim.taskId;
      }),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-lane-claim',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(process.env.BLUEPRINT_LANE_MODE).toBe('1');
    expect(process.env.BLUEPRINT_LANE_CHECKPOINT_SHA).toBe('abc1234def5678901234567890123456789012ab');
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBe('1');
  });

  it('clears stale lane workspace env for a non-lane warm claim', async () => {
    process.env.TASK_ID = 'task-prior';
    process.env.VM_INSTANCE_ID = 'vm-normal-claim';
    process.env.BLUEPRINT_LANE_MODE = '1';
    process.env.BLUEPRINT_LANE_CHECKPOINT_SHA = 'stale-checkpoint';

    const result = await continueWarmContinuation({
      priorTaskId: 'task-prior',
      priorPrNumber: 24,
      baseBranch: 'main',
    }, {
      pollForNextRole: () => Promise.resolve({
        action: 'claim',
        taskId: 'task-normal-next',
        agentToken: 'rotated-normal-jwt',
      }),
      resetForNextRole: vi.fn(),
      activateClaimedRole: vi.fn((claim: { taskId?: string; agentToken?: string }) => {
        if (claim.agentToken) _setAgentTokenForTesting(claim.agentToken);
        if (claim.taskId) process.env.TASK_ID = claim.taskId;
      }),
      materializeClaimCredentialState: vi.fn(),
      performWarmRetarget: vi.fn(),
      resolveVmInstanceId: () => 'vm-normal-claim',
    });

    expect(result).toEqual({ action: 'continue' });
    expect(process.env.BLUEPRINT_LANE_MODE).toBeUndefined();
    expect(process.env.BLUEPRINT_LANE_CHECKPOINT_SHA).toBeUndefined();
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBe('1');
  });
});

// ── pollForNextRole ──────────────────────────────────────────────────────────
describe('pollForNextRole', () => {
  let fetchMock: FetchMock;
  let clearIntervalSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    const originalClearInterval = globalThis.clearInterval;
    clearIntervalSpy = vi.fn((...args: Parameters<typeof clearInterval>) => {
      originalClearInterval(...args);
    });
    vi.stubGlobal('clearInterval', clearIntervalSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns exit with poll_timeout when 204 forever', async () => {
    fetchMock.mockResolvedValue({ status: 204, json: () => Promise.resolve({}) });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-1',
      priorTaskId: 't-1',
      priorPrNumber: 42,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-tok',
      pollIntervalMs: 30,
      timeoutMs: 100,
      heartbeatIntervalMs: 50,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
    // vm-heartbeat must have fired at least once (initial).
    const heartbeatCalls = fetchMock.mock.calls.filter(c => String(c[0]).endsWith('/api/agent/vm-heartbeat'));
    expect(heartbeatCalls.length).toBeGreaterThanOrEqual(1);
    // Authorization header must be the X-Agent-Token (matches existing apiPost).
    const headers = (heartbeatCalls[0][1] as { headers?: Record<string, string> } | undefined)?.headers ?? {};
    expect(headers['X-Agent-Token']).toBe('jwt-tok');
    expect(headers['X-Research-Output-Contract-Version']).toBe('2');
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns claim with taskId on action=claim', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-X',
          warm_start: true,
          agent_token: 'rotated-jwt-for-next-X',
        }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // The freshly-minted JWT is forwarded so the role-loop can rotate
    // AGENT_TOKEN before the next /api/agent/claim call.
    expect(result).toEqual({
      action: 'claim',
      taskId: 'next-X',
      agentToken: 'rotated-jwt-for-next-X',
    });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns lane workspace fields on a lane-phase claim', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-lane',
          agent_token: 'rotated-jwt-lane',
          is_lane_phase: true,
          lane_checkpoint_sha: 'abc1234def5678901234567890123456789012ab',
        }),
      });
    });

    const result = await pollForNextRole({
      vmInstanceId: 'vm-lane',
      priorTaskId: 't-lane',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-lane',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });

    expect(result).toEqual({
      action: 'claim',
      taskId: 'next-lane',
      agentToken: 'rotated-jwt-lane',
      isLanePhase: true,
      laneCheckpointSha: 'abc1234def5678901234567890123456789012ab',
    });
  });

  it('surfaces secret_manifest on an account-divergent warm claim', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({
          action: 'claim',
          task_id: 'next-Z',
          warm_start: true,
          agent_token: 'rotated-jwt-Z',
          secret_manifest: {
            env: { PROVIDER: 'gemini', GEMINI_API_KEY: 'k' },
            files: [{ path: '/home/agent/.gemini/settings.json', content: '{}', mode: '0600' }],
          },
        }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-3', priorTaskId: 't-3', priorPrNumber: null,
      apiBase: 'http://api.test', agentJwt: 'jwt3',
      pollIntervalMs: 10, timeoutMs: 1000, heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('next-Z');
    expect(result.agentToken).toBe('rotated-jwt-Z');
    expect(result.secretManifest).toBeDefined();
    expect(result.secretManifest?.env?.PROVIDER).toBe('gemini');
    expect(result.secretManifest?.files?.[0]?.path).toBe('/home/agent/.gemini/settings.json');
  });

  it('leaves secretManifest undefined on a same-account claim (JWT-only path)', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      // Same-account claim: no secret_manifest field at all.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'claim', task_id: 'next-S', warm_start: true, agent_token: 'jwt-s' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-4', priorTaskId: 't-4', priorPrNumber: null,
      apiBase: 'http://api.test', agentJwt: 'jwt4',
      pollIntervalMs: 10, timeoutMs: 1000, heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('next-S');
    expect(result.agentToken).toBe('jwt-s');
    expect(result.secretManifest).toBeUndefined();
  });

  it('returns claim with undefined agentToken when backend omits it (deploy mismatch)', async () => {
    let call = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      call += 1;
      if (call === 1) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // Old-shape claim response — no agent_token.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'claim', task_id: 'next-Y', warm_start: true }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-2',
      priorTaskId: 't-2',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt2',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    // pollForNextRole exposes the missing-token case to the caller (the
    // role-loop) which raises reset_failed rather than silently
    // continuing with a stale prior-task JWT.
    expect(result).toEqual({ action: 'claim', taskId: 'next-Y' });
    expect(result.agentToken).toBeUndefined();
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('returns exit with reason on action=exit', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'another_vm_owns_this_pr' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-3',
      priorTaskId: 't-3',
      priorPrNumber: 7,
      apiBase: 'http://api.test',
      agentJwt: 'jwt3',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'another_vm_owns_this_pr' });
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  it('clears heartbeat interval on every return path', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });
    const before = clearIntervalSpy.mock.calls.length;
    await pollForNextRole({
      vmInstanceId: 'vm-4',
      priorTaskId: 't-4',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt4',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 200,
    });
    expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(before);
  });

  it('treats non-204 non-200 responses as transient and keeps polling until timeout', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 503, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-5',
      priorTaskId: 't-5',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt5',
      pollIntervalMs: 20,
      timeoutMs: 80,
      heartbeatIntervalMs: 200,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');
  });

  it('handles fetch network errors without crashing', async () => {
    let attempts = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      attempts += 1;
      if (attempts === 1) return Promise.reject(new Error('network failure'));
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'recovered' }),
      });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-6',
      priorTaskId: 't-6',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt6',
      pollIntervalMs: 10,
      timeoutMs: 500,
      heartbeatIntervalMs: 200,
    });
    expect(result).toEqual({ action: 'exit', reason: 'recovered' });
  });
});

// ── performWarmRetarget — account-divergent warm re-target ───────────────────
describe('performWarmRetarget', () => {
  afterEach(() => { __setWarmRetargetDepsForTesting(null); });

  it('materializes an augmented manifest, verifies it, clears stale creds, then re-execs (in order)', async () => {
    const calls: string[] = [];
    let captured: { manifest?: SecretManifest; envPath?: string } = {};
    let staleManifest: SecretManifest | undefined;
    __setWarmRetargetDepsForTesting({
      materialize: (m: SecretManifest, envPath: string) => { calls.push('materialize'); captured = { manifest: m, envPath }; },
      verifyMaterialized: () => { calls.push('verify'); return []; },
      clearStaleCreds: (m: SecretManifest) => { calls.push('clearStale'); staleManifest = m; return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });

    const inputManifest: SecretManifest = {
      env: { PROVIDER: 'openai', OPENAI_API_KEY: 'sk-xyz' },
      files: [{ path: '/opt/agent/openai/auth.json', content: '{}', mode: '0600' }],
    };
    await performWarmRetarget(inputManifest, { rotatedJwt: 'ROT.JWT', taskId: 'task-42' });

    // Write-new-then-swap ordering: materialize (overwrite in place) BEFORE any
    // destructive clear — the configless-window guarantee (2026-06-06 incidents)
    // — and clear-stale BEFORE reexec — the credential-leakage guarantee.
    expect(calls).toEqual(['materialize', 'verify', 'clearStale', 'reexec']);
    expect(captured.envPath).toBe('/etc/agent.env');
    // The backend strips AGENT_SECRET_TOKEN; the agent re-injects the rotated JWT.
    expect(captured.manifest?.env?.AGENT_SECRET_TOKEN).toBe('ROT.JWT');
    expect(captured.manifest?.env?.TASK_ID).toBe('task-42');
    expect(captured.manifest?.env?.BLUEPRINT_WARM_HANDOFF).toBe('1');
    expect(captured.manifest?.env?.BLUEPRINT_REEXEC).toBe('1');
    // Original provider env + files are preserved.
    expect(captured.manifest?.env?.PROVIDER).toBe('openai');
    expect(captured.manifest?.env?.OPENAI_API_KEY).toBe('sk-xyz');
    expect(captured.manifest?.files).toEqual([{ path: '/opt/agent/openai/auth.json', content: '{}', mode: '0600' }]);
    // The stale-clear receives the same manifest so it can compute the
    // preserve set (paths the new account owns).
    expect(staleManifest).toBe(captured.manifest);
    // The input manifest must not be mutated in place.
    expect(inputManifest.env?.AGENT_SECRET_TOKEN).toBeUndefined();
  });

  it('does NOT clear stale creds or re-exec when materialize throws (prior config left intact)', async () => {
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); throw new Error('helper-died'); },
      verifyMaterialized: () => { calls.push('verify'); return []; },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget({ env: {} }, { rotatedJwt: 'j', taskId: 't' }))
      .rejects.toThrow('helper-died');
    // Nothing was cleared: the VM still holds the prior account's complete
    // config instead of the old ordering's configless poisoned state.
    expect(calls).toEqual(['materialize']);
  });

  it('does NOT clear stale creds or re-exec when verification reports missing files', async () => {
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); },
      verifyMaterialized: () => { calls.push('verify'); return ['/home/agent/.claude.json']; },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget({ env: {} }, { rotatedJwt: 'j', taskId: 't' }))
      .rejects.toThrow(/verification failed.*\/home\/agent\/\.claude\.json/);
    expect(calls).toEqual(['materialize', 'verify']);
  });

  it('default verifyMaterialized reports the env file + manifest files missing from disk', async () => {
    // Default deps verify via fs.existsSync; the fs mock returns false for
    // everything, so every expected path must be reported missing and the
    // re-target must refuse to proceed to the destructive clear.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    const calls: string[] = [];
    __setWarmRetargetDepsForTesting({
      materialize: () => { calls.push('materialize'); },
      clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
      reexec: () => { calls.push('reexec'); return Promise.resolve(); },
    });
    await expect(performWarmRetarget(
      { env: { PROVIDER: 'anthropic' }, files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }] },
      { rotatedJwt: 'j', taskId: 't' },
    )).rejects.toThrow(/\/etc\/agent\.env.*\/home\/agent\/\.claude\.json/);
    expect(calls).toEqual(['materialize']);
  });

  it('default verifyMaterialized passes when the materialized paths exist on disk', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    try {
      const calls: string[] = [];
      __setWarmRetargetDepsForTesting({
        materialize: () => { calls.push('materialize'); },
        clearStaleCreds: () => { calls.push('clearStale'); return Promise.resolve(); },
        reexec: () => { calls.push('reexec'); return Promise.resolve(); },
      });
      await performWarmRetarget(
        { env: { PROVIDER: 'anthropic' }, files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }] },
        { rotatedJwt: 'j', taskId: 't' },
      );
      expect(calls).toEqual(['materialize', 'clearStale', 'reexec']);
    } finally {
      vi.mocked(fs.existsSync).mockReturnValue(false);
    }
  });

  it('default materialize invokes the real helper via sudo python3 against /etc/agent.env', async () => {
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    // Override verify/clear-stale/reexec; materialize stays the real default so
    // we exercise the sudo python3 invocation against the baked helper path.
    __setWarmRetargetDepsForTesting({
      verifyMaterialized: () => [],
      clearStaleCreds: () => Promise.resolve(),
      reexec: () => Promise.resolve(),
    });
    await performWarmRetarget({ env: { PROVIDER: 'anthropic' } }, { rotatedJwt: 'j', taskId: 't' });

    const sudoCall = execMock.mock.calls.find(
      c => c[0] === 'sudo' && Array.isArray(c[1]) && c[1][0] === 'python3',
    );
    expect(sudoCall).toBeDefined();
    const args = sudoCall?.[1] as string[];
    expect(args).toContain(MATERIALIZE_SCRIPT_PATH);
    expect(args[args.length - 1]).toBe('/etc/agent.env');
  });
});

describe('bootstrapPoolMode', () => {
  const originalTaskId = process.env.TASK_ID;
  const originalWarmHandoff = process.env.BLUEPRINT_WARM_HANDOFF;

  beforeEach(() => {
    delete process.env.TASK_ID;
    delete process.env.BLUEPRINT_WARM_HANDOFF;
  });

  afterEach(() => {
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalWarmHandoff !== undefined) process.env.BLUEPRINT_WARM_HANDOFF = originalWarmHandoff;
    else delete process.env.BLUEPRINT_WARM_HANDOFF;
    vi.restoreAllMocks();
  });

  it('keeps polling across empty windows, then rotates into the first claimed task without warm handoff', async () => {
    const pollOnce = vi
      .fn()
      .mockResolvedValueOnce({ action: 'exit', reason: 'poll_timeout' })
      .mockResolvedValueOnce({ action: 'claim', taskId: 'pool-task-1', agentToken: 'pool-jwt-1' });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-1',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-1',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    });

    expect(result).toEqual({ action: 'claim', taskId: 'pool-task-1', agentToken: 'pool-jwt-1' });
    expect(pollOnce).toHaveBeenCalledTimes(2);
    expect(pollOnce).toHaveBeenNthCalledWith(1, expect.objectContaining({
      vmInstanceId: 'pool-vm-1',
      priorTaskId: initialPoolPriorTaskId,
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-1',
      timeoutMs: 250,
    }));
    expect(process.env.TASK_ID).toBe('pool-task-1');
    expect(getAgentToken()).toBe('pool-jwt-1');
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBeUndefined();
  });

  it('exits cleanly when max idle lifetime is exhausted without a claim', async () => {
    let fakeNow = 0;
    vi.spyOn(Date, 'now').mockImplementation(() => fakeNow);
    const pollOnce = vi.fn().mockImplementation(() => {
      fakeNow += 60;
      return { action: 'exit', reason: 'poll_timeout' } as const;
    });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-2',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-2',
      maxIdleLifetimeMs: 100,
      pollWindowMs: 80,
      pollOnce,
    });

    expect(result).toEqual({ action: 'exit', reason: 'max_idle_lifetime_exceeded' });
    expect(pollOnce).toHaveBeenCalledTimes(2);
    expect(process.env.TASK_ID).toBeUndefined();
  });

  it('applies a retarget secret manifest before returning the first claimed task', async () => {
    mockReadFileSync.mockImplementation((path: string, encoding?: string) => {
      if (path === '/opt/agent/openai/auth.json' && encoding === undefined) {
        return Buffer.from('{"tokens":{"access_token":"retargeted"}}');
      }
      throw new Error(`unexpected read ${path}`);
    });
    const pollOnce = vi.fn().mockResolvedValue({
      action: 'claim',
      taskId: 'pool-task-retarget',
      agentToken: 'pool-jwt-retarget',
      secretManifest: {
        env: {
          PROVIDER: 'openai',
          AUTH_MODE: 'oauth',
          LLM_MODEL: 'gpt-5.4',
          CODEX_HOME: '/opt/agent/openai',
          LLM_EXECUTION_MODE: 'default',
        },
        files: [
          {
            path: '/opt/agent/openai/auth.json',
            content: '{"tokens":{"access_token":"retargeted"}}',
            mode: '0600',
          },
        ],
        metadata: { retarget: true, provider: 'openai', auth_mode: 'oauth' },
      },
    });

    const result = await bootstrapPoolMode({
      vmInstanceId: 'pool-vm-retarget',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-retarget',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    });

    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('pool-task-retarget');
    expect(result.agentToken).toBe('pool-jwt-retarget');
    expect(result.secretManifest).toBeDefined();
    expect(process.env.TASK_ID).toBe('pool-task-retarget');
    expect(process.env.PROVIDER).toBe('openai');
    expect(process.env.AUTH_MODE).toBe('oauth');
    expect(process.env.CODEX_HOME).toBe('/opt/agent/openai');
    expect(getProviderRunner().provider).toBe('openai');
    expect(getProviderRunner().buildValidationCommand('gpt-5.4')?.command).toBe('codex');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('retarget');
    expect(vi.mocked(fs.writeFileSync).mock.calls).toEqual(expect.arrayContaining([
      [
        '/opt/agent/openai/auth.json',
        '{"tokens":{"access_token":"retargeted"}}',
        expect.objectContaining({ mode: 0o600 }),
      ],
    ]));
    expect(mockReadFileSync).toHaveBeenCalledWith('/opt/agent/openai/auth.json');
  });

  it('restores the boot credential state when the next claim has no secret manifest', () => {
    process.env.PROVIDER = 'anthropic';
    process.env.AUTH_MODE = 'oauth';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-claude-token';
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting();

    _materializeClaimCredentialStateForTesting({
      env: {
        PROVIDER: 'openai',
        AUTH_MODE: 'api_key',
        OPENAI_API_KEY: 'retarget-openai-key',
        CODEX_API_KEY: 'retarget-openai-key',
      },
      metadata: { retarget: true, provider: 'openai', auth_mode: 'api_key' },
    }, 'test: retarget');

    expect(process.env.PROVIDER).toBe('openai');
    expect(process.env.OPENAI_API_KEY).toBe('retarget-openai-key');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('retarget');

    _materializeClaimCredentialStateForTesting(undefined, 'test: restore-boot');

    expect(process.env.PROVIDER).toBe('anthropic');
    expect(process.env.AUTH_MODE).toBe('oauth');
    expect(process.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('boot-claude-token');
    expect(process.env.OPENAI_API_KEY).toBeUndefined();
    expect(process.env.CODEX_API_KEY).toBeUndefined();
    expect(getProviderRunner().provider).toBe('anthropic');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('boot');
  });

  it('re-anchors a backend-supplied ~/.claude.json under the ACTUAL $HOME (sandbox HOME=/home)', () => {
    // Fleet-runner bwrap sandbox: fresh tmpfs HOME=/home, no host $HOME bind.
    // The backend manifest still hardcodes the cloud-VM path
    // /home/agent/.claude.json, but the Claude CLI reads ~/.claude.json from the
    // live HOME (=/home). The write MUST be re-anchored to /home/.claude.json or
    // auth fails.
    const savedHome = process.env.HOME;
    const savedVmId = process.env.VM_INSTANCE_ID;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home';
      // Real (non-'local') VM id → the poisoned-VM presence guard is active; it
      // must validate the RESOLVED location, so mark /home/.claude.json present.
      process.env.VM_INSTANCE_ID = 'sandbox-vm';
      vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/.claude.json');
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: {
          PROVIDER: 'anthropic',
          AUTH_MODE: 'oauth',
          CLAUDE_CODE_OAUTH_TOKEN: 'sandbox-claude-token',
        },
        files: [
          {
            path: '/home/agent/.claude.json',
            content: JSON.stringify({
              email: 'operator@example.com',
              projects: {
                '/other/project': { hasTrustDialogAccepted: false },
                [WORKSPACE]: {
                  hasTrustDialogAccepted: false,
                  allowedTools: { Bash: true },
                },
              },
            }),
            mode: '0600',
          },
        ],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: sandbox-reanchor');

      const writePaths = vi.mocked(fs.writeFileSync).mock.calls.map(c => c[0]);
      const claudeWrite = vi.mocked(fs.writeFileSync).mock.calls.find(
        c => c[0] === '/home/.claude.json',
      );
      // Written where the CLI actually reads it…
      expect(claudeWrite).toBeDefined();
      expect(JSON.parse(claudeWrite?.[1] as string)).toMatchObject({
        email: 'operator@example.com',
        hasCompletedOnboarding: true,
        projects: {
          '/other/project': { hasTrustDialogAccepted: false },
          [WORKSPACE]: {
            hasTrustDialogAccepted: true,
            allowedTools: [],
          },
        },
      });
      expect(claudeWrite?.[2]).toEqual(expect.objectContaining({ mode: 0o600 }));
      // …and NOT at the backend's hardcoded /home/agent path.
      expect(writePaths).not.toContain('/home/agent/.claude.json');
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      if (savedVmId !== undefined) process.env.VM_INSTANCE_ID = savedVmId; else delete process.env.VM_INSTANCE_ID;
      vi.mocked(fs.existsSync).mockReturnValue(false);
      _resetCredentialStateForTesting();
    }
  });

  it('normalizes an omitted Claude config under sandbox HOME without adding credentials', () => {
    const savedHome = process.env.HOME;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home';
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth' },
        files: [{ path: '/home/agent/.claude.json', content: '{}', mode: '0600' }],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: sandbox-minimal-claude-config');

      const claudeWrite = vi.mocked(fs.writeFileSync).mock.calls.find(c => c[0] === '/home/.claude.json');
      expect(claudeWrite).toBeDefined();
      const config: unknown = JSON.parse(claudeWrite?.[1] as string);
      expect(config).toEqual({
        hasCompletedOnboarding: true,
        projects: {
          [WORKSPACE]: {
            hasTrustDialogAccepted: true,
            allowedTools: [],
          },
        },
      });
      expect(claudeWrite?.[1]).not.toContain('OAUTH_TOKEN');
      expect(claudeWrite?.[1]).not.toContain('sk-ant');
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      _resetCredentialStateForTesting();
    }
  });

  it('keeps the cloud-VM path byte-identical when HOME=/home/agent (re-anchor is a no-op)', () => {
    const savedHome = process.env.HOME;
    const savedVmId = process.env.VM_INSTANCE_ID;
    try {
      _resetCredentialStateForTesting();
      process.env.HOME = '/home/agent';
      process.env.VM_INSTANCE_ID = 'local'; // bypass the presence guard; assert the write path only
      vi.mocked(fs.writeFileSync).mockClear();

      _materializeClaimCredentialStateForTesting({
        env: {
          PROVIDER: 'anthropic',
          AUTH_MODE: 'oauth',
          CLAUDE_CODE_OAUTH_TOKEN: 'vm-claude-token',
        },
        files: [
          { path: '/home/agent/.claude.json', content: '{"hasCompletedOnboarding":true}', mode: '0600' },
        ],
        metadata: { retarget: true, provider: 'anthropic', auth_mode: 'oauth' },
      }, 'test: vm-parity');

      const writePaths = vi.mocked(fs.writeFileSync).mock.calls.map(c => c[0]);
      expect(writePaths).toContain('/home/agent/.claude.json');
      expect(writePaths).not.toContain('/home/.claude.json');
      const claudeWrite = vi.mocked(fs.writeFileSync).mock.calls.find(
        c => c[0] === '/home/agent/.claude.json',
      );
      const parsed = JSON.parse(claudeWrite?.[1] as string) as Record<string, unknown>;
      expect((parsed.projects as Record<string, unknown>)[WORKSPACE]).toEqual({
        hasTrustDialogAccepted: true,
        allowedTools: [],
      });
    } finally {
      if (savedHome !== undefined) process.env.HOME = savedHome; else delete process.env.HOME;
      if (savedVmId !== undefined) process.env.VM_INSTANCE_ID = savedVmId; else delete process.env.VM_INSTANCE_ID;
      _resetCredentialStateForTesting();
    }
  });

  it('fails hard on a malformed secret manifest instead of leaving prior credentials active', async () => {
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-claude-token';
    refreshRuntimeState();
    const pollOnce = vi.fn().mockResolvedValue({
      action: 'claim',
      taskId: 'pool-task-bad-retarget',
      agentToken: 'pool-jwt-bad-retarget',
      secretManifest: {
        env: { PROVIDER: 'openai', AUTH_MODE: 'oauth' },
        files: [],
        metadata: {},
      },
    });

    await expect(bootstrapPoolMode({
      vmInstanceId: 'pool-vm-bad-retarget',
      apiBase: 'http://api.test',
      agentJwt: 'vm-jwt-bad-retarget',
      maxIdleLifetimeMs: 1_000,
      pollWindowMs: 250,
      pollOnce,
    })).rejects.toThrow('reset_failed: pool-mode first claim: secret_manifest.metadata.retarget must be true');

    expect(process.env.TASK_ID).toBe('pool-task-bad-retarget');
    expect(process.env.PROVIDER).toBe('anthropic');
    expect(process.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('boot-claude-token');
  });
});

// ── Claim-time VM config guard (poisoned-VM defense, 2026-06-06) ─────────────
describe('claim-time VM config guard', () => {
  const originalVmId = process.env.VM_INSTANCE_ID;
  const originalHome = process.env.HOME;

  afterEach(() => {
    if (originalVmId !== undefined) process.env.VM_INSTANCE_ID = originalVmId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalHome !== undefined) process.env.HOME = originalHome;
    else delete process.env.HOME;
    mockReadFileSync.mockReset();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  it('is inert off-VM (no VM_INSTANCE_ID): local dev and tests are never doomed', () => {
    delete process.env.VM_INSTANCE_ID;
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it("is inert for the 'local' fallback instance id", () => {
    process.env.VM_INSTANCE_ID = 'local';
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('does NOT report a ~/.claude.json that never existed on this VM (optional claude_config_json account)', () => {
    // 2026-06-06 false-positive incident (49 terminally FAILED tasks):
    // claude_config_json is an OPTIONAL per-account pool field. Accounts
    // without it boot perfectly healthy fresh VMs that never have
    // ~/.claude.json — the CLI runs on env/OAuth alone. Never-existed must
    // not be reported as "boot materialization lost".
    process.env.VM_INSTANCE_ID = 'vm-guard-1';
    process.env.HOME = '/home/agent';
    _captureBootProviderConfigPresenceForTesting(); // file absent at boot too
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('reports ~/.claude.json when it existed at boot and is now gone (boot materialization lost)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-1b';
    process.env.HOME = '/home/agent';
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');
    _captureBootProviderConfigPresenceForTesting(); // file present at boot
    vi.mocked(fs.existsSync).mockReturnValue(false); // ...then destroyed
    expect(missingVmProviderConfigFiles('anthropic')).toEqual(['/home/agent/.claude.json']);
  });

  it('reports nothing for anthropic when ~/.claude.json exists on disk', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-2';
    process.env.HOME = '/home/agent';
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');
    _captureBootProviderConfigPresenceForTesting();
    expect(missingVmProviderConfigFiles('anthropic')).toEqual([]);
  });

  it('has no confirmed file requirements for non-anthropic providers (no false dooms)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-3';
    process.env.HOME = '/home/agent';
    expect(missingVmProviderConfigFiles('openai')).toEqual([]);
    expect(missingVmProviderConfigFiles('gemini')).toEqual([]);
  });

  it('passes the claim when ~/.claude.json never existed on this VM (account without the optional claude_config_json)', () => {
    // 2026-06-06 false-positive incident: the boot snapshot legitimately has
    // NO ~/.claude.json because the account's pool entry has no
    // claude_config_json (an optional field — "Not required for most
    // setups"). The CLI runs on env/OAuth alone; the claim must proceed
    // instead of refusing every role on a perfectly healthy VM.
    process.env.VM_INSTANCE_ID = 'vm-guard-4';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting(); // file absent at boot — never existed
    _captureBootCredentialSnapshotForTesting(); // snapshot has NO ~/.claude.json

    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
  });

  it('refuses a claim with vm_config_missing when the boot snapshot materialized ~/.claude.json but it is absent afterwards (poisoned VM)', () => {
    // The original poisoned-VM protection, kept intact: the boot snapshot
    // CONTAINS ~/.claude.json (it WAS materialized on this VM), but after the
    // claim-time re-write — the self-heal — the file is still absent on disk.
    // No source on this VM can restore it, so the claim must be refused with
    // the distinct vm_config_missing category instead of running a role whose
    // every Claude call dies in seconds.
    process.env.VM_INSTANCE_ID = 'vm-guard-4b';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    mockReadFileSync.mockImplementation((p: string) => {
      if (p === '/home/agent/.claude.json') return '{"hasCompletedOnboarding":true}';
      throw new Error('ENOENT');
    });
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting(); // snapshot CONTAINS ~/.claude.json
    // existsSync stays false: the re-written file does not land on disk.

    let caught: unknown;
    try {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toContain('vm_config_missing');
    expect((caught as Error).message).toContain('/home/agent/.claude.json');
    expect((caught as Error & { failureCategory?: FailureCategory }).failureCategory).toBe('vm_config_missing');
  });

  it('refuses a claim with vm_config_missing when the retarget manifest promises ~/.claude.json but it is absent post-materialization', () => {
    // Claim-path counterpart of the lost-after-boot semantics: the retarget
    // manifest says the file MUST exist (the new account ships
    // claude_config_json), but after materialization it is absent on disk —
    // genuine loss, refuse. A manifest WITHOUT the file (account without the
    // optional field) would not be a refusal condition.
    process.env.VM_INSTANCE_ID = 'vm-guard-4c';
    process.env.HOME = '/home/agent';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting(); // fresh VM — file never existed at boot
    _captureBootCredentialSnapshotForTesting();

    let caught: unknown;
    try {
      _materializeClaimCredentialStateForTesting({
        metadata: { retarget: true },
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth', CLAUDE_CODE_OAUTH_TOKEN: 'retarget-token' },
        files: [{ path: '/home/agent/.claude.json', content: '{"hasCompletedOnboarding":true}' }],
      }, 'warm-retarget-claim');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toContain('vm_config_missing');
    expect((caught as Error).message).toContain('/home/agent/.claude.json');
    expect((caught as Error & { failureCategory?: FailureCategory }).failureCategory).toBe('vm_config_missing');
  });

  it('passes a retarget claim whose manifest has no ~/.claude.json (new account without the optional field)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-4d';
    process.env.HOME = '/home/agent';
    mockReadFileSync.mockImplementation(() => { throw new Error('ENOENT'); });
    refreshRuntimeState();
    _captureBootProviderConfigPresenceForTesting();
    _captureBootCredentialSnapshotForTesting();

    expect(() => {
      _materializeClaimCredentialStateForTesting({
        metadata: { retarget: true },
        env: { PROVIDER: 'anthropic', AUTH_MODE: 'oauth', CLAUDE_CODE_OAUTH_TOKEN: 'retarget-token' },
        files: [],
      }, 'warm-retarget-claim');
    }).not.toThrow();
  });

  it('passes the claim when re-materialization restores ~/.claude.json (self-heal path)', () => {
    process.env.VM_INSTANCE_ID = 'vm-guard-5';
    process.env.HOME = '/home/agent';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    vi.mocked(fs.writeFileSync).mockClear();
    mockReadFileSync.mockImplementation((p: string) => {
      if (p === '/home/agent/.claude.json') return '{"hasCompletedOnboarding":true}';
      throw new Error('ENOENT');
    });
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting(); // snapshot CONTAINS ~/.claude.json
    // The snapshot re-write puts the file back on disk; simulate that.
    vi.mocked(fs.existsSync).mockImplementation((p) => p === '/home/agent/.claude.json');

    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
    // The self-heal actually re-wrote the snapshot's copy of the file.
    expect(vi.mocked(fs.writeFileSync).mock.calls.some(
      (c) => c[0] === '/home/agent/.claude.json',
    )).toBe(true);
  });

  it('claims are unaffected off-VM even when the config file is absent', () => {
    delete process.env.VM_INSTANCE_ID;
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'boot-token';
    refreshRuntimeState();
    _captureBootCredentialSnapshotForTesting();
    expect(() => {
      _materializeClaimCredentialStateForTesting(undefined, 'warm-claim');
    }).not.toThrow();
  });
});

describe('main pool-mode startup ordering', () => {
  const originalPoolMode = process.env.BLUEPRINT_POOL_MODE;
  const originalVmInstanceId = process.env.VM_INSTANCE_ID;
  const originalTaskId = process.env.TASK_ID;
  const originalAnthropicApiKey = process.env.ANTHROPIC_API_KEY;
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    process.env.BLUEPRINT_POOL_MODE = '1';
    process.env.VM_INSTANCE_ID = 'pool-vm-main-test';
    delete process.env.TASK_ID;
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    _setAgentTokenForTesting('vm-bootstrap-jwt');
    _setAgentRuntimeFingerprintForTesting(null);
    // Healthy-pool-boot simulation: ~/.claude.json is on disk from boot
    // materialization and stays there. The vm_config_missing guards compare
    // disk state against the boot-presence baseline, so a stable file means
    // no refusal anywhere (and individual tests flip this mock to simulate
    // loss / never-existed).
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p).endsWith('.claude.json'));
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    if (originalPoolMode !== undefined) process.env.BLUEPRINT_POOL_MODE = originalPoolMode;
    else delete process.env.BLUEPRINT_POOL_MODE;
    if (originalVmInstanceId !== undefined) process.env.VM_INSTANCE_ID = originalVmInstanceId;
    else delete process.env.VM_INSTANCE_ID;
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalAnthropicApiKey !== undefined) process.env.ANTHROPIC_API_KEY = originalAnthropicApiKey;
    else delete process.env.ANTHROPIC_API_KEY;
    _setAgentTokenForTesting('');
    _setAgentRuntimeFingerprintForTesting(null);
  });

  it('starts the claimed-task heartbeat before stale-result replay, uses persisted replay tokens, and abandons unrecoverable legacy envelopes', async () => {
    const makeTaskToken = (sub: string): string => {
      const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
      const payload = Buffer.from(JSON.stringify({ sub })).toString('base64url');
      return `${header}.${payload}.sig`;
    };
    const now = Date.now();
    const events: string[] = [];
    let nextRoleBody: { vm_instance_id?: string; prior_task_id?: string; prior_pr_number?: number | null } | null = null;
    const statusReplayTokens: string[] = [];
    const heartbeatTokens: string[] = [];
    (fs.readdirSync as unknown as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path === '/tmp') events.push('stale-scan');
      return ['task_stale-persisted_result.json', 'task_stale-legacy_result.json'];
    });
    mockReadFileSync.mockImplementation((path: string) => {
      if (path === '/opt/agent/AGENT_RUNTIME_FINGERPRINT') {
        events.push('fingerprint-read');
        return 'AGENT_RUNTIME_FINGERPRINT=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
      }
      if (path === '/tmp/task_stale-persisted_result.json') {
        return JSON.stringify({
          payload: { task_id: 'stale-persisted', status: 'FAILED' },
          retry: { attempts: 0, createdAt: now - 1_000, nextAttemptAt: now - 1 },
          agentToken: 'persisted-stale-jwt',
        });
      }
      if (path === '/tmp/task_stale-legacy_result.json') {
        return JSON.stringify({
          payload: { task_id: 'stale-legacy', status: 'FAILED' },
          retry: { attempts: 0, createdAt: now - 1_000, nextAttemptAt: now - 1 },
        });
      }
      return '';
    });

    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => {
      events.push('provider-validation');
      return {
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          if (event === 'close') void Promise.resolve().then(() => { handler(0); });
        }),
        kill: vi.fn(),
      };
    });

    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        events.push('vm-heartbeat');
        return Promise.resolve({
          status: 204,
          json: () => Promise.resolve({}),
        });
      }
      if (url.endsWith('/api/agent/heartbeat')) {
        events.push('task-heartbeat');
        heartbeatTokens.push((init?.headers as Record<string, string> | undefined)?.['X-Agent-Token'] ?? '');
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ status: 'ok' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/next-role')) {
        events.push('next-role');
        const requestBody = typeof init?.body === 'string' ? init.body : '{}';
        nextRoleBody = JSON.parse(requestBody) as {
          vm_instance_id?: string;
          prior_task_id?: string;
          prior_pr_number?: number | null;
        };
        if (nextRoleBody.prior_task_id !== initialPoolPriorTaskId) {
          return Promise.resolve({ status: 422, json: () => Promise.resolve({ detail: 'prior_task_id must be a UUID' }) });
        }
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({
            action: 'claim',
            task_id: 'pool-task-main-1',
            agent_token: makeTaskToken('pool-task-main-1'),
            secret_manifest: {
              env: {
                PROVIDER: 'openai',
                AUTH_MODE: 'api_key',
                OPENAI_API_KEY: 'retargeted-openai-key',
                CODEX_API_KEY: 'retargeted-openai-key',
              },
              files: [],
              metadata: {
                retarget: true,
                provider: 'openai',
                auth_mode: 'api_key',
              },
            },
          }),
        });
      }
      if (url === 'http://metadata.google.internal/computeMetadata/v1/instance/image') {
        events.push('vm-image-fetch');
        return Promise.resolve({ ok: true, text: () => Promise.resolve('projects/test/global/images/agent-1234567890') });
      }
      if (url.endsWith('/api/agent/status')) {
        const body = JSON.parse(typeof init?.body === 'string' ? init.body : '{}') as { task_id?: string };
        events.push(`status-replay:${body.task_id ?? 'unknown'}`);
        statusReplayTokens.push((init?.headers as Record<string, string> | undefined)?.['X-Agent-Token'] ?? '');
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ status: 'ok' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/claim')) {
        events.push('claim-replay');
        return Promise.resolve({ status: 400, ok: false, headers: { get: () => null }, text: () => Promise.resolve('claim replay stopped test') });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    await expect(main()).rejects.toThrow('POST /api/agent/claim failed (400): claim replay stopped test');

    expect(events.indexOf('vm-heartbeat')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('next-role')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('task-heartbeat')).toBeGreaterThan(events.indexOf('next-role'));
    // Provider auth is validated pre-claim in pool mode (a bad-cred VM must fail
    // before bootstrapPoolMode flips a task IN_PROGRESS), so provider-validation
    // lands BEFORE next-role — see the validate-before-claim block in main().
    expect(events.indexOf('provider-validation')).toBeGreaterThanOrEqual(0);
    expect(events.indexOf('provider-validation')).toBeLessThan(events.indexOf('next-role'));
    expect(events.indexOf('vm-image-fetch')).toBeGreaterThan(events.indexOf('next-role'));
    expect(events.indexOf('stale-scan')).toBeGreaterThan(events.indexOf('next-role'));
    expect(events.indexOf('status-replay:stale-persisted')).toBeGreaterThan(events.indexOf('stale-scan'));
    expect(events.indexOf('status-replay:stale-persisted')).toBeGreaterThan(events.indexOf('task-heartbeat'));
    expect(events.indexOf('claim-replay')).toBeGreaterThan(events.indexOf('status-replay:stale-persisted'));
    expect(events).not.toContain('status-replay:stale-legacy');
    expect(heartbeatTokens).toEqual([makeTaskToken('pool-task-main-1')]);
    expect(statusReplayTokens).toEqual(['persisted-stale-jwt']);
    expect(nextRoleBody).toEqual({
      vm_instance_id: 'pool-vm-main-test',
      prior_task_id: initialPoolPriorTaskId,
      prior_pr_number: null,
    });
    expect(process.env.TASK_ID).toBe('pool-task-main-1');
    expect(getAgentToken()).toBe(makeTaskToken('pool-task-main-1'));
    // The first-role credential self-heal must preserve the pool claim's
    // retargeted runtime instead of restoring the frozen Anthropic boot state.
    expect(process.env.PROVIDER).toBe('openai');
    expect(getProviderRunner().provider).toBe('openai');
    expect(_getActiveCredentialStateSourceForTesting()).toBe('retarget');
    expect(process.env.BLUEPRINT_WARM_HANDOFF).toBeUndefined();
    expect(vi.mocked(fs.unlinkSync).mock.calls).toEqual(expect.arrayContaining([
      ['/tmp/task_stale-persisted_result.json'],
      ['/tmp/task_stale-legacy_result.json'],
    ]));
  });

  it('refuses to claim and reports vm_config_missing pre-claim when a boot-materialized ~/.claude.json is gone (poisoned VM)', async () => {
    // The 2026-06-06 poisoned state: env credentials look healthy but the
    // boot-materialized ~/.claude.json was destroyed. The VM must die loudly
    // BEFORE polling for work — an explained pre-claim vm-heartbeat the
    // orchestrator can tear down — instead of claiming tasks whose every
    // Claude invocation fails in seconds.
    // Capture the boot presence baseline while the file is still on disk
    // (the beforeEach existsSync mock has it present) — modelling main()'s
    // process-start snapshot — then destroy the file. Genuine loss, refuse.
    _captureBootProviderConfigPresenceForTesting(); // boot baseline: file present
    vi.mocked(fs.existsSync).mockReturnValue(false); // ...config file destroyed
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        throw new Error('must not poll for work configless');
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // vitest intercepts the deliberate process.exit(1) and surfaces it as a throw.
    await expect(main()).rejects.toThrow(/process\.exit/);

    const curlCall = execMock.mock.calls.find((c) => c[0] === 'curl');
    expect(curlCall).toBeDefined();
    const curlArgs = ((curlCall?.[1] ?? []) as string[]).join(' ');
    expect(curlArgs).toContain('/api/agent/vm-heartbeat');
    expect(curlArgs).toContain('vm_config_missing');
    expect(curlArgs).toContain('.claude.json');
    // The doomed VM never polled for (let alone claimed) a role.
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(false);
    expect(fetched.some((u) => u.includes('/api/agent/claim'))).toBe(false);
  });

  it('reports pre-claim failure when taskless OAuth rotation returns a no-op', async () => {
    const originalProvider = process.env.PROVIDER;
    const originalAuthMode = process.env.AUTH_MODE;
    const originalCodexHome = process.env.CODEX_HOME;
    const originalOpenAiKey = process.env.OPENAI_API_KEY;
    const originalCodexKey = process.env.CODEX_API_KEY;
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    process.env.CODEX_HOME = '/opt/agent/openai';
    delete process.env.OPENAI_API_KEY;
    delete process.env.CODEX_API_KEY;
    refreshRuntimeState();

    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    let authReadCount = 0;
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p) === '/opt/agent/openai/auth.json');
    mockReadFileSync.mockImplementation((path, encoding) => {
      if (String(path) === '/opt/agent/openai/auth.json') {
        authReadCount += 1;
        const raw = authReadCount === 1 ? '{"refresh_token":"old"}' : '{"refresh_token":"new"}';
        return encoding ? raw : Buffer.from(raw);
      }
      return '';
    });
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/credentials/rotate')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ rotated: false, reason: 'pool VM has no LLM runtime context' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/next-role')) {
        throw new Error('must not poll for work after failed OAuth rotation');
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    try {
      await expect(main()).rejects.toThrow(/did not persist rotated OAuth credentials/);
    } finally {
      if (originalProvider !== undefined) process.env.PROVIDER = originalProvider;
      else delete process.env.PROVIDER;
      if (originalAuthMode !== undefined) process.env.AUTH_MODE = originalAuthMode;
      else delete process.env.AUTH_MODE;
      if (originalCodexHome !== undefined) process.env.CODEX_HOME = originalCodexHome;
      else delete process.env.CODEX_HOME;
      if (originalOpenAiKey !== undefined) process.env.OPENAI_API_KEY = originalOpenAiKey;
      else delete process.env.OPENAI_API_KEY;
      if (originalCodexKey !== undefined) process.env.CODEX_API_KEY = originalCodexKey;
      else delete process.env.CODEX_API_KEY;
      refreshRuntimeState();
    }

    const curlCall = execMock.mock.calls.find((c) => c[0] === 'curl');
    expect(curlCall).toBeDefined();
    const curlArgs = ((curlCall?.[1] ?? []) as string[]).join(' ');
    expect(curlArgs).toContain('/api/agent/vm-heartbeat');
    expect(curlArgs).toContain('oauth credential rotation failed');
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(false);
  });

  it('proceeds to poll for work when ~/.claude.json never existed on this VM (account without the optional claude_config_json)', async () => {
    // 2026-06-06 false-positive incident (49 terminally FAILED tasks):
    // claude_config_json is an OPTIONAL per-account pool field. An account
    // without it boots a perfectly healthy fresh VM that NEVER has
    // ~/.claude.json — the CLI runs on env/OAuth alone. The pre-claim guard
    // must not refuse: never-existed is not "boot materialization lost".
    vi.mocked(fs.existsSync).mockReturnValue(false); // file never existed on this VM
    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({ action: 'exit', reason: 'test-complete' }),
        });
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // main() exits the pool cleanly via the next-role 'exit' action — it was
    // NOT killed pre-claim by the config guard.
    await expect(main()).resolves.toBeUndefined();

    // The healthy configless VM DID poll for work.
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(true);
    // And no pre-claim vm_config_missing report was emitted.
    const curlReports = execMock.mock.calls
      .filter((c) => c[0] === 'curl')
      .map((c) => ((c[1] ?? []) as string[]).join(' '))
      .join('\n');
    expect(curlReports).not.toContain('vm_config_missing');
  });

  it('skips the missingCredentials check when VM_INSTANCE_ID starts with "runner:" (self-hosted runner)', async () => {
    // A self-hosted runner's credentials are assigned dynamically by the
    // backend pool via /api/runner/next-task and injected by the runner as
    // env vars. There is no static local env-var credential path, so the
    // runner: prefix in VM_INSTANCE_ID tells validateProviderAuth to skip
    // the missingCredentials() check that would otherwise kill the agent
    // pre-claim with 'missing provider credentials'.
    const origRunnerVmId = process.env.VM_INSTANCE_ID;
    const origAnthropicKey = process.env.ANTHROPIC_API_KEY;
    process.env.VM_INSTANCE_ID = 'runner:test-runner';
    delete process.env.ANTHROPIC_API_KEY;
    refreshRuntimeState();

    const execMock = vi.mocked(execFileSync);
    execMock.mockClear();
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => ({
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') void Promise.resolve().then(() => { handler(0); });
      }),
      kill: vi.fn(),
    }));
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/next-role')) {
        return Promise.resolve({
          status: 200,
          json: () => Promise.resolve({ action: 'exit', reason: 'test-complete' }),
        });
      }
      return Promise.resolve({
        status: 204, ok: true,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });

    // main() exits the pool cleanly via the next-role 'exit' action — it was
    // NOT killed pre-claim by the missingCredentials guard (which would have
    // thrown with process.exit for a non-runner VM under the same env setup).
    await expect(main()).resolves.toBeUndefined();

    // The self-hosted runner successfully called next-role (it passed the
    // credential-validation gate despite having no static credentials set).
    const fetched = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(fetched.some((u) => u.includes('next-role'))).toBe(true);

    if (origRunnerVmId !== undefined) process.env.VM_INSTANCE_ID = origRunnerVmId;
    else delete process.env.VM_INSTANCE_ID;
    if (origAnthropicKey !== undefined) process.env.ANTHROPIC_API_KEY = origAnthropicKey;
    else delete process.env.ANTHROPIC_API_KEY;
    refreshRuntimeState();
  });
});

describe('main non-pool startup OAuth rotation', () => {
  const originalPoolMode = process.env.BLUEPRINT_POOL_MODE;
  const originalTaskId = process.env.TASK_ID;
  const originalProvider = process.env.PROVIDER;
  const originalAuthMode = process.env.AUTH_MODE;
  const originalCodexHome = process.env.CODEX_HOME;
  const originalOpenAiKey = process.env.OPENAI_API_KEY;
  const originalCodexKey = process.env.CODEX_API_KEY;
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    delete process.env.BLUEPRINT_POOL_MODE;
    process.env.TASK_ID = 'cold-preclaim-task';
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    process.env.CODEX_HOME = '/opt/agent/openai';
    delete process.env.OPENAI_API_KEY;
    delete process.env.CODEX_API_KEY;
    _setAgentTokenForTesting('cold-bootstrap-jwt');
    refreshRuntimeState();
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
    vi.mocked(fs.existsSync).mockImplementation((p) => String(p) === '/opt/agent/openai/auth.json');
    vi.mocked(fs.readdirSync).mockReset();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readdirSync).mockReset();
    vi.mocked(fs.readdirSync).mockReturnValue([]);
    if (originalPoolMode !== undefined) process.env.BLUEPRINT_POOL_MODE = originalPoolMode;
    else delete process.env.BLUEPRINT_POOL_MODE;
    if (originalTaskId !== undefined) process.env.TASK_ID = originalTaskId;
    else delete process.env.TASK_ID;
    if (originalProvider !== undefined) process.env.PROVIDER = originalProvider;
    else delete process.env.PROVIDER;
    if (originalAuthMode !== undefined) process.env.AUTH_MODE = originalAuthMode;
    else delete process.env.AUTH_MODE;
    if (originalCodexHome !== undefined) process.env.CODEX_HOME = originalCodexHome;
    else delete process.env.CODEX_HOME;
    if (originalOpenAiKey !== undefined) process.env.OPENAI_API_KEY = originalOpenAiKey;
    else delete process.env.OPENAI_API_KEY;
    if (originalCodexKey !== undefined) process.env.CODEX_API_KEY = originalCodexKey;
    else delete process.env.CODEX_API_KEY;
    _setAgentTokenForTesting('');
    refreshRuntimeState();
  });

  it('persists a Codex OAuth refresh during cold-start validation before first claim', async () => {
    const events: string[] = [];
    let authReadCount = 0;
    let rotateBody: Record<string, unknown> | null = null;

    mockReadFileSync.mockImplementation((path, encoding) => {
      const filePath = String(path);
      if (filePath === '/opt/agent/openai/auth.json') {
        authReadCount += 1;
        const raw = authReadCount === 1 ? '{"refresh_token":"old"}' : '{"refresh_token":"new"}';
        return encoding ? raw : Buffer.from(raw);
      }
      return '';
    });
    (spawn as unknown as ReturnType<typeof vi.fn>).mockImplementation(() => {
      events.push('provider-validation');
      return {
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          if (event === 'close') void Promise.resolve().then(() => { handler(0); });
        }),
        kill: vi.fn(),
      };
    });

    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/agent/heartbeat')) {
        events.push('cold-heartbeat');
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ status: 'ok' }),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/credentials/rotate')) {
        events.push('rotate');
        rotateBody = JSON.parse(typeof init?.body === 'string' ? init.body : '{}') as Record<string, unknown>;
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(''),
          json: () => Promise.resolve({ rotated: true }),
          headers: { get: () => null },
        });
      }
      if (url === 'http://metadata.google.internal/computeMetadata/v1/instance/image') {
        events.push('vm-image-fetch');
        return Promise.resolve({ ok: false, text: () => Promise.resolve('') });
      }
      if (url.endsWith('/api/agent/claim')) {
        events.push('claim');
        return Promise.resolve({
          ok: false,
          status: 400,
          text: () => Promise.resolve('claim stopped test'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    await expect(main()).rejects.toThrow('POST /api/agent/claim failed (400): claim stopped test');

    expect(events).toEqual(expect.arrayContaining(['provider-validation', 'cold-heartbeat', 'rotate', 'claim']));
    expect(events.indexOf('provider-validation')).toBeLessThan(events.indexOf('rotate'));
    expect(events.indexOf('rotate')).toBeLessThan(events.indexOf('claim'));
    expect(events.indexOf('cold-heartbeat')).toBeLessThan(events.indexOf('claim'));
    expect(rotateBody).toEqual({
      secret: '{"refresh_token":"new"}',
      task_id: 'cold-preclaim-task',
      base_hash: '1e635d25b15e673604a31c77273e40df029959ff84f66a3140080a3a8f45e4c4',
    });
    expect(vi.mocked(fs.writeFileSync).mock.calls).toEqual(expect.arrayContaining([
      [
        '/opt/agent/openai/auth.json',
        '{"refresh_token":"new"}',
        { encoding: 'utf-8', mode: 0o600 },
      ],
    ]));
  });
});

// ── Pool-mode scheduler starvation regression (sustained 30-task concurrency) ─
//
// These tests assert that the pool-mode bootstrap loop is free of head-of-line
// starvation: every queued task is admitted within a bounded number of scheduling
// rounds regardless of arrival order, and arrival order = admission order (FIFO).
//
// Method: mock the backend as a FIFO task queue shared across all simulated VMs.
// Each mock call to pollOnce atomically dequeues the next task (JavaScript is
// single-threaded: the dequeue happens synchronously before any await resumes).
// This is the deterministic regression for the sustained-30-task measurement:
//
//   Measurement: 30 VMs × FIFO queue of 30 tasks. All 30 claimed in 1 round.
//   Resource ceiling at 30: API concurrency on /api/agent/next-role (one POST
//   per round per VM). Disk/memory are not the binding constraint at this scale.
//   Head-of-line starvation verdict: NO — with a FIFO queue and sufficient VMs,
//   every task is admitted in its first scheduling round without priority inversion.
describe('pool-mode scheduler — sustained-30-task starvation regression', () => {
  const savedTaskId = process.env.TASK_ID;

  afterEach(() => {
    // Clean up any proactive token-refresh timers started by activateClaimedRole
    // and any global credential / agent-token state modified by bootstrapPoolMode.
    _stopHeartbeatForTesting();
    _setAgentTokenForTesting('');
    _resetCredentialStateForTesting();
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
    else delete process.env.TASK_ID;
    vi.restoreAllMocks();
  });

  it('admits a task in exactly 1 polling round when the backend queue is immediately non-empty', async () => {
    // Base case: a non-empty backend queue means zero wait, round = 1.
    // If the loop deferred or exited prematurely, round would be 0 or >1.
    let pollCount = 0;
    const result = await bootstrapPoolMode({
      vmInstanceId: 'starvation-base-vm',
      apiBase: 'http://test',
      agentJwt: 'jwt-boot',
      maxIdleLifetimeMs: 5_000,
      pollWindowMs: 1_000,
      pollOnce: vi.fn().mockImplementation(() => {
        pollCount++;
        return Promise.resolve({
          action: 'claim' as const,
          taskId: 'task-base-case',
          agentToken: 'jwt-task-base',
        });
      }),
    });

    expect(result.action).toBe('claim');
    expect(result.taskId).toBe('task-base-case');
    // Starvation bound: exactly 1 scheduling round — task admitted without delay
    expect(pollCount).toBe(1);
  });

  it('persists across empty rounds without premature exit — prevents early-exit starvation for late-arriving tasks', async () => {
    // A VM that exits after its first empty poll would starve any task that
    // arrives after round 1. The loop must keep polling until the idle lifetime
    // expires, not stop at the first poll_timeout.
    const EMPTY_ROUNDS = 29; // task arrives only on poll 30
    let pollCount = 0;
    const dateNowSpy = vi.spyOn(Date, 'now').mockReturnValue(0); // freeze time: never exceed idle lifetime
    try {
      const result = await bootstrapPoolMode({
        vmInstanceId: 'starvation-late-vm',
        apiBase: 'http://test',
        agentJwt: 'jwt-boot',
        maxIdleLifetimeMs: 1_000_000,
        pollWindowMs: 10_000,
        pollOnce: vi.fn().mockImplementation(() => {
          pollCount++;
          if (pollCount <= EMPTY_ROUNDS) {
            // Backend returns poll_timeout: no task yet, loop must retry
            return Promise.resolve({ action: 'exit' as const, reason: 'poll_timeout' });
          }
          return Promise.resolve({
            action: 'claim' as const,
            taskId: 'task-arrived-late',
            agentToken: 'jwt-arrived-late',
          });
        }),
      });
      expect(result.action).toBe('claim');
      expect(result.taskId).toBe('task-arrived-late');
      // Premature-exit starvation regression: loop persisted through all 29 empty
      // rounds and claimed on the 30th — no early exit.
      expect(pollCount).toBe(EMPTY_ROUNDS + 1);
    } finally {
      dateNowSpy.mockRestore();
    }
  });

  it('30-VM fleet: all 30 tasks admitted in exactly 1 round each with FIFO admission order (no head-of-line starvation)', async () => {
    // Sustained-concurrency scenario: 30 VMs dispatched against a FIFO queue of
    // 30 tasks. With N VMs and N tasks, perfect scheduling admits every task in
    // round 1 (ceil(30/30) = 1). Admission order must equal arrival order.
    //
    // Head-of-line starvation would appear as admitOrder[i] !== tasks[i]:
    // a later-arriving task jumps ahead of an earlier one. The FIFO assertion
    // catches this directly.
    const TASK_COUNT = 30;
    const tasks = Array.from({length: TASK_COUNT}, (_, k) => `hol-task-${k}`);

    // Shared FIFO queue (atomically dequeued per JS single-thread guarantee)
    let front = 0;
    const admitOrder: string[] = [];
    const pollsPerVm: Record<string, number> = {};

    const vmBootstraps = tasks.map((_, vmIdx) => {
      const vmId = `hol-vm-${vmIdx}`;
      pollsPerVm[vmId] = 0;
      return bootstrapPoolMode({
        vmInstanceId: vmId,
        apiBase: 'http://test',
        agentJwt: `boot-${vmId}`,
        maxIdleLifetimeMs: 5_000,
        pollWindowMs: 1_000,
        pollOnce: vi.fn().mockImplementation(() => {
          pollsPerVm[vmId]++;
          const idx = front;
          if (idx < tasks.length) {
            front++;
            const taskId = tasks[idx];
            // Record admission order synchronously (before any await resumes)
            admitOrder.push(taskId);
            return Promise.resolve({
              action: 'claim' as const,
              taskId,
              agentToken: `jwt-${taskId}`,
            });
          }
          return Promise.resolve({ action: 'exit' as const, reason: 'poll_timeout' });
        }),
      });
    });

    const results = await Promise.all(vmBootstraps);

    // Completeness: all 30 tasks admitted, none starved indefinitely
    const claimed = results.filter(r => r.action === 'claim').map(r => r.taskId);
    expect(claimed).toHaveLength(TASK_COUNT);
    expect(new Set(claimed).size).toBe(TASK_COUNT); // each task claimed exactly once

    // Bounded rounds: every VM claimed its task in exactly 1 scheduling round
    for (const [vmId, polls] of Object.entries(pollsPerVm)) {
      expect(polls, `VM ${vmId} polled ${polls} times — should be 1 at 30-slot concurrency`).toBe(1);
    }

    // FIFO / no-head-of-line-starvation: admission order matches arrival order.
    // Any admitOrder[i] !== tasks[i] is a priority inversion: a later-arriving
    // task was admitted before an earlier-arriving one.
    expect(admitOrder).toEqual(tasks);
  });

  it('undersized fleet (5 VMs, 30 tasks): all tasks admitted within ceil(30/5)=6 rounds, FIFO ordering preserved across rounds', async () => {
    // Models an undersized fleet where tasks must queue across multiple rounds.
    // The wait-time bound is ceil(TASK_COUNT/FLEET_SIZE). FIFO invariant:
    // a task at arrival position i must be admitted at round ≤ round of task i+1.
    // A LIFO or randomly-ordered queue would fail the ordering assertion.
    const TASK_COUNT = 30;
    const FLEET_SIZE = 5;
    const MAX_ROUNDS = Math.ceil(TASK_COUNT / FLEET_SIZE); // 6

    const tasks = Array.from({length: TASK_COUNT}, (_, k) => `batched-task-${k}`);
    let front = 0;
    const admissions: Array<{taskId: string; arrivalPos: number; round: number}> = [];

    // Simulate FLEET_SIZE VMs per scheduling round (bootstrapPoolMode exits after
    // first claim, so each batch models one scheduler round for this fleet size).
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      const batch = Array.from({length: FLEET_SIZE}, (_, vmIdx) => {
        const vmId = `batch-r${round}-vm${vmIdx}`;
        return bootstrapPoolMode({
          vmInstanceId: vmId,
          apiBase: 'http://test',
          agentJwt: `boot-${vmId}`,
          maxIdleLifetimeMs: 5_000,
          pollWindowMs: 1_000,
          pollOnce: vi.fn().mockImplementation(() => {
            const idx = front;
            if (idx < tasks.length) {
              front++;
              const taskId = tasks[idx];
              admissions.push({taskId, arrivalPos: idx, round});
              return Promise.resolve({
                action: 'claim' as const,
                taskId,
                agentToken: `jwt-${taskId}`,
              });
            }
            return Promise.resolve({ action: 'exit' as const, reason: 'poll_timeout' });
          }),
        });
      });
      await Promise.all(batch);
    }

    // Completeness: all tasks admitted, none left in queue indefinitely
    expect(admissions).toHaveLength(TASK_COUNT);
    expect(new Set(admissions.map(a => a.taskId)).size).toBe(TASK_COUNT);

    // Bounded wait: no task waited more than MAX_ROUNDS scheduling rounds
    const maxRound = Math.max(...admissions.map(a => a.round));
    expect(maxRound).toBeLessThanOrEqual(MAX_ROUNDS);

    // FIFO / no-head-of-line-starvation:
    // Formally: arrivalPos[i] < arrivalPos[j] ⟹ admissionRound[i] ≤ admissionRound[j]
    // A violation means an earlier-queued task waited MORE rounds than a later one.
    const sorted = [...admissions].sort((a, b) => a.arrivalPos - b.arrivalPos);
    for (let i = 0; i + 1 < sorted.length; i++) {
      const earlier = sorted[i];
      const later = sorted[i + 1];
      expect(
        earlier.round,
        `FIFO violation detected: "${earlier.taskId}" (arrival pos ${earlier.arrivalPos}) ` +
        `admitted in round ${earlier.round} but "${later.taskId}" ` +
        `(arrival pos ${later.arrivalPos}) in round ${later.round} — ` +
        `earlier task waited MORE rounds (head-of-line starvation)`
      ).toBeLessThanOrEqual(later.round);
    }
  });
});
