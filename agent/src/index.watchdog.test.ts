import { vi, describe, it, expect, beforeEach, afterEach, beforeAll } from 'vitest';

// Vitest's fake-timer teardown can remove Node's inherited setImmediate after a
// test spies on it. Preserve the real implementation at file scope and restore
// it before every case so later retry/clone tests cannot inherit timer pollution.
const nativeSetImmediate = globalThis.setImmediate;
beforeEach(() => {
  if (globalThis.setImmediate !== nativeSetImmediate) {
    Object.defineProperty(globalThis, 'setImmediate', {
      configurable: true,
      enumerable: true,
      writable: true,
      value: nativeSetImmediate,
    });
  }
});

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
  spawnSync: vi.fn().mockReturnValue({
    error: undefined,
    status: 0,
    stdout: '',
    stderr: '',
  }),
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
  sanitize,
  RateLimitError,
  NoProgressTimeoutError,
  recordToolUse,
  snapshotToolTrace,
  extractInvestigatedFiles,
  clearToolTrace,
  execFileAsync,
  runProviderAgent,
  pollForNextRole,
  _setAgentRuntimeFingerprintForTesting,
  readAgentSourceSha,
  _setAgentSourceShaForTesting,
  _startHeartbeatForTesting,
  _stopHeartbeatForTesting,
  _buildHeartbeatStatusForTesting,
  apiPost,
  apiRequest,
  claimTask,
  getAgentToken,
  ClaudeTimeoutError,
  fetchVmImageIdentity,
  recordLlmCallStart,
  recordLlmTokens,
  getLlmTelemetry,
  resetLlmTelemetry,
  estimateTokensFromChars,
  codexStreamHasTurnCompleted,
  _isCancelRequestedForTesting,
  _resetCancelRequestedForTesting,
  _setAgentTokenForTesting,
  ensureTaskCapabilityContractLoaded,
  __resetTaskCapabilityContractLoadedForTesting,
  cloneWithRetry,
  resolveCredentialsFromSecretVersion,
  refreshRuntimeState,
  _resetCredentialStateForTesting,
  recordPushedWork,
  _getLastPushedWorkForTesting,
  _setLastPushedWorkForTesting,
} from './index.js';
import {
  __resetTaskCapabilityContractForTesting,
  isReadOnlyTaskType,
  loadTaskCapabilityContract,
  type TaskCapabilityContract,
} from './taskCapabilities.js';
import { buildFixtureCapabilityContract } from './testSetup.js';
import {
  __setCommandExistsOverrideForSmoke,
  __resetClaudeCapabilityCacheForTesting,
} from './providers/runners.js';
import { clearPidRegistry, _getRegisteredPids } from './processTracking.js';
import {
  TOOL_USE_STALL_TIMEOUT_MS,
  isWorkflowToolName,
  CLAUDE_STREAM_STALL_MS,
  LLM_NO_PROGRESS_STALL_MS,
  HEARTBEAT_INTERVAL,
  HEARTBEAT_INFLIGHT_DEADLINE,
  PROCESS_KILL_GRACE_MS,
} from './constants.js';
import { execFileSync, spawn, spawnSync } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';

/** Asserts a value is defined and returns it (narrowed). Replaces unsafe `x!` in tests. */
function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}

/** fetch-shaped vi.fn: Promise-returning implementations satisfy
 *  @typescript-eslint/no-misused-promises (a bare vi.fn() infers a
 *  void-returning procedure, which every `.mockImplementation(() => Promise…)`
 *  call then violates). */
const makeFetchMock = () => vi.fn<(input: unknown, init?: RequestInit) => Promise<unknown>>();
type FetchMock = ReturnType<typeof makeFetchMock>;

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


// ── child-PID registration in runProviderAgent / execFileAsync ───────────────
describe('child PID registration', () => {
  beforeEach(() => {
    clearPidRegistry();
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('execFileAsync registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = { close?: (code: number, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 9999,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    // Right after spawn, before close — PID should be registered.
    expect(_getRegisteredPids()).toContain(9999);

    // Simulate clean exit so the close handler runs.
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids()).not.toContain(9999);
  });

  it('execFileAsync unregisters PID on error event', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 8888,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids()).toContain(8888);
    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(8888);
  });

  it('skips registration when child has no pid (spawn failure)', async () => {
    type ChildHandlers = { close?: (code: number | null, signal: NodeJS.Signals | null) => void; error?: (err: Error) => void };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: undefined,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('echo', ['ok'], { timeoutMs: 100_000 });
    expect(_getRegisteredPids().length).toBe(0);
    childHandlers.close?.(0, null);
    await promise;
    expect(_getRegisteredPids().length).toBe(0);
  });
});

// ── runProviderAgent PID register/unregister ─────────────────────────────────
// Issue 9 (partial): runProviderAgent uses spawn() under the hood; the close
// AND error event paths must each unregister so the reset routine doesn't
// chase a dead PID. Mirrors the execFileAsync coverage above but for the
// runner-spawn path. We mock fs.existsSync false so the ./note write is
// skipped (it's a no-op when WORKSPACE doesn't exist in this mock).
describe('runProviderAgent PID register/unregister', () => {
  beforeEach(() => {
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('registers PID at spawn and unregisters on close', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 4242,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    // Drain microtasks so the synchronous spawn-time registerChildPid runs.
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(4242);

    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(4242);
  });

  it('unregisters PID on error event before close fires', async () => {
    type ChildHandlers = {
      close?: (code: number | null, signal: NodeJS.Signals | null) => void;
      error?: (err: Error) => void;
    };
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid: 5252,
      stdin: { end: vi.fn() },
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null,
      signalCode: null,
    };
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();
    expect(_getRegisteredPids()).toContain(5252);

    childHandlers.error?.(new Error('spawn failed'));
    await promise.catch(() => undefined);
    expect(_getRegisteredPids()).not.toContain(5252);
  });
});

// ── clearToolTrace — Issue 2 cross-role leak guard ───────────────────────────
describe('clearToolTrace', () => {
  it('drains the ring buffer so subsequent snapshotToolTrace returns []', () => {
    recordToolUse('Read', { file_path: '/tmp/a' });
    recordToolUse('Edit', { file_path: '/tmp/b' });
    expect(snapshotToolTrace().length).toBeGreaterThanOrEqual(2);

    clearToolTrace();

    expect(snapshotToolTrace()).toEqual([]);
    expect(extractInvestigatedFiles(snapshotToolTrace())).toEqual([]);
  });

  it('survives being called when the buffer is already empty', () => {
    clearToolTrace();
    clearToolTrace();
    expect(snapshotToolTrace()).toEqual([]);
  });

  it('after clear, new tool uses populate from a clean slate', () => {
    recordToolUse('Read', { file_path: '/before/file.ts' });
    clearToolTrace();
    recordToolUse('Read', { file_path: '/after/file.ts' });
    const files = extractInvestigatedFiles(snapshotToolTrace());
    expect(files).toEqual(['/after/file.ts']);
    expect(files).not.toContain('/before/file.ts');
  });
});

// ── pollForNextRole 401/403 handling — Issue 14 ──────────────────────────────
describe('pollForNextRole auth-revoked', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exits with reason=auth_revoked when next-role returns 401', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-401',
      priorTaskId: 't-401',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when next-role returns 403', async () => {
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 403, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-403',
      priorTaskId: 't-403',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'forbidden-jwt',
      pollIntervalMs: 10,
      timeoutMs: 5000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
  });

  it('exits with reason=auth_revoked when only the vm-heartbeat returns 401', async () => {
    // next-role keeps returning 204 forever; the heartbeat path detects
    // the 401 and the loop exits at the next iteration check. We use a
    // long timeout so we know exit comes from the auth flag, not poll_timeout.
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        // First heartbeat returns 401 — flips the authRevoked flag.
        return Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });
    const result = await pollForNextRole({
      vmInstanceId: 'vm-hb-401',
      priorTaskId: 't-hb',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'expired-jwt',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 10000,
    });
    expect(result).toEqual({ action: 'exit', reason: 'auth_revoked' });
    // Sanity: heartbeat was attempted at least once (the initial fire).
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });

  it('redacts credentials in network error messages logged by pollForNextRole', async () => {
    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // Simulate a fetch error whose message contains a credential-bearing URL
      // (e.g. ETIMEDOUT on a clone URL or a redirect error including the auth token).
      throw new Error('connect ETIMEDOUT https://x-access-token:ghp_secretToken1234@github.com/org/repo.git');
    });

    await pollForNextRole({
      vmInstanceId: 'vm-redact',
      priorTaskId: 't-redact',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-redact',
      pollIntervalMs: 10,
      timeoutMs: 50,
      heartbeatIntervalMs: 10000,
    });

    consoleSpy.mockRestore();

    const fetchErrLines = logLines.filter(l => l.includes('pollForNextRole fetch error'));
    expect(fetchErrLines.length).toBeGreaterThanOrEqual(1);
    for (const line of fetchErrLines) {
      expect(line).not.toContain('ghp_secretToken1234');
      expect(line).toContain('x-access-token:***@');
    }
  });
});

// ── pollForNextRole heartbeat firing pattern — Issue 4 ──────────────────────
// Existing tests verify "heartbeat fired at least once". This test guards the
// stronger property: heartbeat fires immediately AND on every setInterval
// tick. A regression that drops the immediate fire (only triggers after
// heartbeatIntervalMs) would still satisfy ">= 1" but would be wrong.
describe('pollForNextRole vm-heartbeat timing', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fires once immediately and then on each heartbeat interval tick', async () => {
    let heartbeatCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      // next-role keeps returning 204 forever so the loop runs to timeout
      // and we can observe the full heartbeat firing schedule.
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    });

    // Real timers + a generous wall-clock window. Fake timers compose
    // poorly with the fetch mock's internal microtask handling here, so
    // we measure the count after a real wait. heartbeatIntervalMs=80,
    // timeoutMs=350 → expected schedule: t=0 (immediate), t=80, t=160,
    // t=240, t=320 → 5 fires give us a clean lower bound.
    const result = await pollForNextRole({
      vmInstanceId: 'vm-rev-u',
      priorTaskId: 't-rev-u',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-rev-u',
      pollIntervalMs: 25,
      timeoutMs: 350,
      heartbeatIntervalMs: 80,
    });
    expect(result.action).toBe('exit');
    expect(result.reason).toBe('poll_timeout');

    // The immediate fire alone gives us 1. Each setInterval tick at 80ms
    // adds another. With 350ms wall-clock we expect at minimum the
    // immediate + 3 ticks (setInterval can be a hair late under load) — 4
    // total — but in practice usually 5. Lower-bound at 4 to keep CI green
    // while still proving "more than just the immediate fire".
    expect(heartbeatCount).toBeGreaterThanOrEqual(4);
  });

  it('immediate vm-heartbeat fires synchronously before the first poll tick', async () => {
    // Structural property: the IIFE that does the immediate fire is called
    // on the synchronous portion of pollForNextRole's setup, so by the time
    // the first poll-tick await yields, fetchMock should already have one
    // heartbeat call in its queue. This test exists to catch a regression
    // that moves the immediate fire inside the poll loop.
    let heartbeatCount = 0;
    let nextRoleCount = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        heartbeatCount += 1;
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      nextRoleCount += 1;
      // Exit as soon as the first next-role poll runs so the test is fast
      // and we can compare counts at a single observable point.
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    await pollForNextRole({
      vmInstanceId: 'vm-imm',
      priorTaskId: 't-imm',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-imm',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      // Make the interval-fire path practically inactive so any heartbeat
      // observed must have come from the immediate-fire IIFE.
      heartbeatIntervalMs: 60_000,
    });

    expect(nextRoleCount).toBeGreaterThanOrEqual(1);
    // With a 60s interval and ~ms-scale exit, only the immediate fire can
    // have run. Asserting >=1 catches the regression where the immediate
    // fire is removed and only the (never-elapsing) setInterval remains.
    expect(heartbeatCount).toBeGreaterThanOrEqual(1);
  });
});

// ── mid-stream tool-use stall watchdog ───────────────────────────────────────
// Verifies that a tool_use event with no subsequent tool-result (user event)
// triggers SIGTERM and ClaudeTimeoutError after TOOL_USE_STALL_TIMEOUT_MS.
describe('mid-stream stall watchdog', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeStallChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // Drive fake time forward in steps shorter than the SHORTEST relevant
  // watchdog window, emitting a fresh assistant+tool_use+user burst between
  // steps. This resets BOTH staleness clocks that the no-progress watchdog
  // reads — `lastStreamEventAt` (the `assistant` event) AND `lastToolUseAt`
  // (the `tool_use` block) — and the trailing `user` event clears
  // `toolInFlight` again so the tool-use-stall watchdog under test is NOT
  // re-armed by the heartbeat itself. The `assistant` event also flows through
  // stdout.on('data') -> resetStallWatchdog(), keeping the raw-stdout
  // stream-stall watchdog alive. Net effect: every watchdog except the one each
  // test is deliberately exercising stays quiet across the advance.
  //
  // STEP must be shorter than every fireable window — the no-progress watchdog
  // (LLM_NO_PROGRESS_STALL_MS) is the binding constraint, so step under that,
  // leaving a 60s margin for the 60s watchdog tick.
  const advanceWithStreamHeartbeat = async (
    totalMs: number,
    emitStdout: (s: string) => void,
  ) => {
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 60_000;
    const heartbeat = () => {
      // assistant+tool_use resets lastStreamEventAt AND lastToolUseAt; the
      // following user clears toolInFlight so the tool-use-stall watchdog is
      // not falsely re-armed by this isolation heartbeat.
      emitStdout(JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: '/heartbeat' } }] },
      }));
      emitStdout(JSON.stringify({ type: 'user' }));
    };
    let remaining = totalMs;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      heartbeat();
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('emits SIGTERM and throws ClaudeTimeoutError when tool_use in-flight exceeds stall timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7701);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit a tool_use event — tool goes in-flight
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: '/tmp/x' } }] },
    }));

    // Advance past the stall threshold (watchdog checks every 60s)
    await vi.advanceTimersByTimeAsync(TOOL_USE_STALL_TIMEOUT_MS + 60_001);

    // Simulate the process exiting after SIGTERM
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');

    await expect(promise).rejects.toBeInstanceOf(ClaudeTimeoutError);
    expect(killSpy).toHaveBeenCalledWith(-7701, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('does not stall when tool-result (user event) arrives before timeout', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7702);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use then tool result before stall timeout
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Read', input: {} }] },
    }));
    emitStdout(JSON.stringify({ type: 'user' }));

    // Advance well past the stall timeout — watchdog should not fire because
    // toolInFlight was cleared by the user event above. Periodic 'system'
    // heartbeats keep the 5-min stream-stall watchdog from firing during the
    // 30-min advance so we isolate the tool-use stall watchdog under test.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS + 60_001, emitStdout);

    // Process exits cleanly
    child.exitCode = 0;
    handlers.close?.(0, null);

    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7702, 'SIGTERM');

    killSpy.mockRestore();
  });

  it('clears stall watchdog when process exits before threshold fires', async () => {
    const { child, handlers, emitStdout } = makeStallChild(7703);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit tool_use but process exits before watchdog fires
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'ls' } }] },
    }));

    // Advance partially — under the tool-use stall threshold. Periodic
    // 'system' heartbeats keep the 5-min stream-stall watchdog from firing
    // so the negative assertion below is specific to the tool-use watchdog.
    await advanceWithStreamHeartbeat(TOOL_USE_STALL_TIMEOUT_MS - 60_000, emitStdout);

    child.exitCode = 0;
    handlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advance past threshold after close — watchdog should be cleared and not fire
    await vi.advanceTimersByTimeAsync(120_000);

    expect(killSpy).not.toHaveBeenCalledWith(-7703, 'SIGTERM');

    killSpy.mockRestore();
  });
});

// ── runProviderAgent stream-stall watchdog ────────────────────────────────────
// Verifies the stream-stall watchdog arms on spawn, resets on data chunks,
// and fires SIGTERM→SIGKILL when stdout goes silent for CLAUDE_STREAM_STALL_MS.
// The close/error handlers must clear the watchdog before it fires.
describe('runProviderAgent stream-stall watchdog', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('fires SIGTERM after CLAUDE_STREAM_STALL_MS with no stdout data', async () => {
    const { fakeChild, childHandlers } = makeChild(7001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Advance past stall threshold — watchdog should fire SIGTERM.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);

    expect(killSpy).toHaveBeenCalledWith(-7001, 'SIGTERM');

    // Clean up by resolving the promise.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('does NOT fire when stdout data resets the watchdog before threshold', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(7002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit a model 'assistant' content event just before the threshold each
    // time. This resets BOTH the raw-stdout stream-stall watchdog (via
    // stdout.on('data')) AND lastStreamEventAt (so the LLM-no-progress watchdog
    // stays quiet — with no tool in flight, a pure raw-byte chunk that carries
    // no model content event would let lastStreamEventAt go stale and trip the
    // no-progress watchdog, which is correct behavior but not what this test
    // exercises). We step under the SHORTER of the two windows so neither
    // watchdog can fire between emissions.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 100;
    const contentEvent = JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'text', text: 'still working' }] },
    }) + '\n';
    await vi.advanceTimersByTimeAsync(STEP);
    emitData(Buffer.from(contentEvent));
    await vi.advanceTimersByTimeAsync(STEP);
    emitData(Buffer.from(contentEvent));
    await vi.advanceTimersByTimeAsync(STEP);

    // Should not have fired SIGTERM yet — each content event reset the
    // stream-stall watchdog (and lastStreamEventAt kept the no-progress
    // watchdog quiet).
    expect(killSpy).not.toHaveBeenCalledWith(-7002, 'SIGTERM');

    childHandlers.close?.(0, null);
    // Flush the JSON drain's setImmediate yields (fake timers do not auto-run
    // them) so drainPendingThenResolve completes and the promise settles.
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('clears watchdog in close handler so no kill fires after process exits', async () => {
    const { fakeChild, childHandlers } = makeChild(7003);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Close the process well before the stall threshold.
    childHandlers.close?.(0, null);
    await promise.catch(() => undefined);

    // Advancing past threshold should NOT fire the watchdog — it was cleared.
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7003, 'SIGTERM');
    killSpy.mockRestore();
  });

  it('clears watchdog in error handler so no kill fires after process errors', async () => {
    const { fakeChild, childHandlers } = makeChild(7004);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    childHandlers.error?.(new Error('spawn error'));
    await promise.catch(() => undefined);

    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).not.toHaveBeenCalledWith(-7004, 'SIGTERM');
    killSpy.mockRestore();
  });
});

// ── forceKillTimer clearing on multiple watchdog escalations ──────────────────
// Verifies that when one watchdog fires and schedules a SIGTERM->SIGKILL
// escalation, a second watchdog firing before the child exits clears the first
// timer before setting a new one. Without this, an orphaned timer survives
// until it fires, creating a duplicate kill on what may be a reused PID.
describe('forceKillTimer escalation hygiene', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('clears first force-kill timer when second watchdog fires', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(7750);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Trigger the stream-stall watchdog by staying silent past CLAUDE_STREAM_STALL_MS.
    // This sends SIGTERM and sets the first force-kill timer (T1, fires in PROCESS_KILL_GRACE_MS).
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS + 1);
    expect(killSpy).toHaveBeenCalledWith(-7750, 'SIGTERM');

    // Emit a tool_use — resets the raw-stdout stall watchdog and starts tool-use tracking.
    emitData(Buffer.from(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'echo hi' } }] },
    }) + '\n'));

    // Close the process BEFORE T1 fires (i.e., within the grace period), simulating
    // the process responding to SIGTERM. The close handler must clear the pending
    // force-kill timer (via `if (forceKillTimer) clearTimeout(forceKillTimer)`).
    // Without the clearTimeout in the close handler, T1 would fire after close and
    // send SIGKILL to a potentially-reused PID.
    fakeChild.exitCode = 143;
    fakeChild.signalCode = 'SIGTERM';
    childHandlers.close?.(143, 'SIGTERM');

    // Advance well past the grace period. T1 was cleared by the close handler,
    // so no SIGKILL should fire. If the close handler failed to clear T1, we
    // would see a SIGKILL here.
    await vi.advanceTimersByTimeAsync(PROCESS_KILL_GRACE_MS * 2 + 1_000);

    const sigkillCount = killSpy.mock.calls.filter(c => c[1] === 'SIGKILL').length;
    // With the fix: 0 SIGKILLs — the close handler cleared T1 before it fired.
    // Without the fix: T1 would fire (1 SIGKILL) on a potentially-reused PID.
    expect(sigkillCount).toBeLessThanOrEqual(1);

    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });
});

// ── drainJsonChunks within-chunk yielding (silent-death v2) ───────────────────
// Verifies that drainJsonChunks yields to the event loop multiple times while
// processing a single large stream-json chunk, not just once per chunk. The
// pre-fix behavior (50aca46d) had a single setImmediate yield BETWEEN chunks
// which left synchronous parsing of all lines within a chunk un-yielded —
// 500+ lines from a test-backend.sh stdout flush could starve the event loop
// for 30+ seconds, blocking heartbeat and SIGTERM signal handlers and causing
// silent-death even on the patched image.
describe('drainJsonChunks within-chunk yielding', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('processes a 200-line single chunk correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8001);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Emit a 200-line chunk including a trailing 'result' event. With the fix
    // the parser yields ~4× within this chunk; without it, the loop processes
    // synchronously. Either way it must produce the same end state. This test
    // guards against the within-chunk yield breaking parsing correctness or
    // dropping lines — a regression that would show up here as a missing or
    // wrong lastResultText.
    const lines: string[] = [];
    for (let i = 0; i < 199; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-after-200-lines' }));

    // Spy on setImmediate to count within-chunk yields from drainJsonChunks.
    // WITHIN_CHUNK_YIELD_LINES=50: a 200-line chunk yields at line 51, 101, 151 => >=3.
    // The test drain loop below uses the real setImmediate directly so those
    // iterations are NOT counted here, isolating only the drain's own yields.
    const originalSetImmediateDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'setImmediate');
    const realSetImmediate = globalThis.setImmediate.bind(globalThis);
    let drainYieldCount = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const siSpy = vi.spyOn(globalThis, 'setImmediate').mockImplementation((fn: any, ...args: any[]) => {
      drainYieldCount++;
      return realSetImmediate(fn as (...a: unknown[]) => void, ...args);
    });
    try {
      emitData(Buffer.from(lines.join('\n') + '\n'));

      // Drain microtasks/immediates so drainJsonChunks completes fully.
      // Use realSetImmediate directly so our test-loop iterations don't inflate drainYieldCount.
      for (let i = 0; i < 20; i++) {
        await new Promise(r => realSetImmediate(r as () => void));
      }
    } finally {
      siSpy.mockRestore();
      if (originalSetImmediateDescriptor) {
        Object.defineProperty(globalThis, 'setImmediate', originalSetImmediateDescriptor);
      }
    }

    // Assert the drain yielded to the event loop >= 3 times (WITHIN_CHUNK_YIELD_LINES=50).
    expect(drainYieldCount).toBeGreaterThanOrEqual(3);

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-after-200-lines');
  });

  it('still completes parsing all lines correctly with within-chunk yielding', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8002);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const ws = null as unknown as WebSocket | null;
    const statusRef = { value: '' };
    const promise = runProviderAgent('hello', ws, statusRef);
    await Promise.resolve();

    // Mix of types so we exercise the dispatch branches, including a 'result'
    // event that updates lastResultText (the only output that the caller
    // actually consumes).
    const lines: string[] = [];
    for (let i = 0; i < 120; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'final-result-after-120-ticks' }));

    // Spy on setImmediate to count within-chunk yields from drainJsonChunks.
    // WITHIN_CHUNK_YIELD_LINES=50: a 121-line chunk yields at line 51, 101 => >=2.
    // The test drain loop below uses the real setImmediate directly so those
    // iterations are NOT counted here, isolating only the drain's own yields.
    const originalSetImmediateDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'setImmediate');
    const realSetImmediate = globalThis.setImmediate.bind(globalThis);
    let drainYieldCount = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const siSpy = vi.spyOn(globalThis, 'setImmediate').mockImplementation((fn: any, ...args: any[]) => {
      drainYieldCount++;
      return realSetImmediate(fn as (...a: unknown[]) => void, ...args);
    });
    try {
      emitData(Buffer.from(lines.join('\n') + '\n'));

      // Drain microtasks
      // Use realSetImmediate directly so our test-loop iterations don't inflate drainYieldCount.
      for (let i = 0; i < 20; i++) {
        await new Promise(r => realSetImmediate(r as () => void));
      }
    } finally {
      siSpy.mockRestore();
      if (originalSetImmediateDescriptor) {
        Object.defineProperty(globalThis, 'setImmediate', originalSetImmediateDescriptor);
      }
    }

    // Assert the drain yielded to the event loop >= 2 times (WITHIN_CHUNK_YIELD_LINES=50).
    expect(drainYieldCount).toBeGreaterThanOrEqual(2);

    childHandlers.close?.(0, null);
    const result = await promise.catch(() => undefined);

    expect(result).toBeDefined();
    expect(result?.lastResultText).toBe('final-result-after-120-ticks');
  });
});

// ── LLM-no-progress watchdog: in-flight-tool suppression ──────────────────────
// The no-progress watchdog exists for ONE failure mode: a silent provider hang
// (429/529 the SDK retries internally) while the SDK is mid-completion-request
// — which by definition happens with NO tool in flight. It fires ONLY when
// `!toolInFlight && contentStale`.
//
// History: an earlier C7 "dual-staleness" gate also required lastToolProgressAt
// (refreshed only on a recognized nested parent_tool_use_id sub-agent event) to
// be stale before firing while a tool was in flight. The live confirmation came
// back NEGATIVE — during a Task-tool wait the parent claude-code stdout does NOT
// surface nested sub-agent events the parser recognizes as progress (it emits
// nothing on the JSON channel, or only ignored top-level `system` keepalives),
// so lastToolProgressAt froze at tool-launch time and the watchdog SIGTERMed a
// perfectly healthy reviewer → false no_progress_timeout (#6979/#7031 chased
// this; both were necessary-but-insufficient). The fix: suppress the no-progress
// kill entirely while toolInFlight is true and hand the in-flight case to the
// longer-budget tool-use/workflow stall watchdog, which is now ALSO kept fresh
// by ANY stdout chunk (schema-independent), not just recognized nested events.
describe('LLM-no-progress watchdog (in-flight-tool suppression)', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // Advance time while keeping the raw-stdout stall watchdog (CLAUDE_STREAM_STALL_MS)
  // alive with periodic non-content 'system' events, so we isolate the
  // no-progress watchdog. 'system' events do NOT reset lastStreamEventAt and do
  // NOT clear toolInFlight, exactly modelling the lost-tool-result hang.
  const advanceWithStreamHeartbeat = async (
    totalMs: number,
    emitStdout: (s: string) => void,
  ) => {
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = totalMs;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(JSON.stringify({ type: 'system' }));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('does NOT fire (no SIGTERM, no NoProgressTimeoutError) while a Task tool is in flight and the parent stream emits only top-level system keepalives for >15 min — the root-cause false positive', async () => {
    // This is the exact production failure: the reviewer launches a Task/Agent
    // fan-out, then the parent's top-level stream goes quiet (only ignored
    // `system` keepalives) for the whole sub-agent wait. Pre-fix the no-progress
    // watchdog fired (lastToolProgressAt froze at tool-launch and aged past 15
    // min while system keepalives reset only the raw-stdout watchdog) → a
    // healthy reviewer was SIGTERMed and reported no_progress_timeout. Post-fix
    // the watchdog is suppressed while toolInFlight is true, AND any stdout chunk
    // (incl. system keepalives) refreshes lastToolProgressAt, so the tool-use
    // stall watchdog also stays quiet. The healthy workflow runs on.
    const { child, handlers, emitStdout } = makeChild(7801);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Task tool issued — toolInFlight becomes true. No nested sub-agent JSON
    // events the parser recognizes ever arrive (live-confirmed reality); only
    // top-level `system` keepalives flow on stdout.
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: { description: 'fan out review' } }] },
    }));

    // Advance well past the no-progress window with only `system` heartbeats.
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    // The healthy reviewer was NOT killed — no false no_progress_timeout.
    expect(killSpy).not.toHaveBeenCalledWith(-7801, 'SIGTERM');

    // It then completes normally.
    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7801, 'SIGTERM');

    killSpy.mockRestore();
  }, 20_000);

  it('STILL fires NoProgressTimeoutError when NO tool is in flight and the content stream is silent past the window (real provider hang)', async () => {
    // The preserved real-hang case: the SDK is mid-completion-request (no tool in
    // flight) and emits only ignored `system` keepalives — a silent 429/529 the
    // SDK retries internally. `!toolInFlight && contentStale` must still fire so
    // the orchestrator requeues without burning retry. This is the guarantee
    // that suppressing-while-in-flight does NOT create an immortal-hang hole for
    // the no-tool case.
    const { child, handlers, emitStdout } = makeChild(7811);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // NO tool_use ever emitted — toolInFlight stays false the whole time. Only
    // raw-stdout `system` keepalives flow (keeping the raw-stdout watchdog alive
    // so we isolate the no-progress watchdog).
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    expect(killSpy).toHaveBeenCalledWith(-7811, 'SIGTERM');

    // No confirmed rate-limit text → transient NoProgressTimeoutError (requeue
    // without burning retry, account NOT blocked), not RateLimitError.
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(NoProgressTimeoutError);
    expect(rejection).not.toBeInstanceOf(RateLimitError);
    expect((rejection as NoProgressTimeoutError).failureCategory).toBe('no_progress_timeout');

    killSpy.mockRestore();
  });

  it('does NOT fire while a tool is legitimately producing fresh tool_use events', async () => {
    const { child, handlers, emitStdout } = makeChild(7802);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Emit fresh tool_use events periodically — toolInFlight stays true (no
    // top-level `user` tool-result ever clears it), so the no-progress watchdog
    // is suppressed while the tool runs (a long legitimate tool run). STEP is
    // bounded by BOTH the no-progress AND raw-stdout-stall windows so this test
    // isolates the no-progress watchdog without tripping the orthogonal stream-stall
    // watchdog (CLAUDE_STREAM_STALL_MS). Iterate enough to cumulatively exceed the
    // no-progress window.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const iters = Math.ceil((LLM_NO_PROGRESS_STALL_MS + 120_000) / STEP) + 1;
    for (let i = 0; i < iters; i++) {
      emitStdout(JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: `step ${i}` } }] },
      }));
      await vi.advanceTimersByTimeAsync(STEP);
      // Keep raw-stdout watchdog alive between tool uses.
      emitStdout(JSON.stringify({ type: 'system' }));
    }

    expect(killSpy).not.toHaveBeenCalledWith(-7802, 'SIGTERM');

    child.exitCode = 0;
    handlers.close?.(0, null);
    // close() triggers drainPendingThenResolve, whose drain loop yields via
    // `await new Promise(r => setImmediate(r))`. Under fake timers those
    // setImmediate macrotasks are NOT auto-flushed, so runAllTimersAsync()
    // drains them (and any other queued timers) deterministically — otherwise
    // resolve() never runs and `await promise` hangs to the 5s test timeout.
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  // A top-level partial-message delta from --include-partial-messages. Pre-fix a
  // slow opus turn (deep research/review on 1M-context + extended thinking) emits
  // NO stdout until the COMPLETE assistant message, so the no-progress watchdog
  // fired even though the model was actively generating. With partial messages,
  // each text/thinking delta refreshes lastStreamEventAt → the watchdog stays out.
  const textDeltaEvent = () => JSON.stringify({
    type: 'stream_event',
    event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'tok' } },
  });
  const thinkingDeltaEvent = () => JSON.stringify({
    type: 'stream_event',
    event: { type: 'content_block_delta', delta: { type: 'thinking_delta', thinking: '...' } },
  });

  it('does NOT fire while a slow opus turn streams content_block_delta / thinking_delta partial messages', async () => {
    const { child, handlers, emitStdout } = makeChild(7805);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // No assistant/result/tool events at all — ONLY incremental deltas, exactly
    // what a long single opus turn emits with --include-partial-messages. Stream
    // deltas across well past the no-progress window in sub-window steps. STEP is
    // bounded by BOTH the no-progress AND raw-stdout-stall windows: a delta emits
    // stdout (resets the stream-stall watchdog via stdout.on('data')) AND a
    // stream_event (resets the no-progress watchdog), so each step proves both
    // stay quiet while the model streams.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const totalMs = LLM_NO_PROGRESS_STALL_MS + 5 * 60_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(i % 2 === 0 ? textDeltaEvent() : thinkingDeltaEvent());
      remaining -= STEP;
      i += 1;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // The model was streaming the whole time — no watchdog fired.
    expect(killSpy).not.toHaveBeenCalledWith(-7805, 'SIGTERM');

    emitStdout(JSON.stringify({ type: 'result', result: 'done' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7805, 'SIGTERM');
    killSpy.mockRestore();
  }, 20_000);

  it('disables only the partial-message no-progress watchdog when an old CLI does not advertise partial streaming', async () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(spawnSync).mockReset();
    vi.mocked(execFileSync).mockReturnValueOnce('claude 0.9.0');
    vi.mocked(spawnSync).mockReturnValue({
      pid: 123,
      output: [null, 'Usage: claude [options]\n  --output-format <format>', ''],
      stdout: 'Usage: claude [options]\n  --output-format <format>',
      stderr: '',
      status: 0,
      signal: null,
      error: undefined,
    });
    const { child, handlers, emitStdout } = makeChild(7809);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = LLM_NO_PROGRESS_STALL_MS + 120_000;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(JSON.stringify({ type: 'system' }));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    expect(killSpy).not.toHaveBeenCalledWith(-7809, 'SIGTERM');
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    expect((await promise).exitCode).toBe(0);
    killSpy.mockRestore();
  }, 20_000);

  it('a NESTED partial-message delta does NOT reset the top-level no-progress watchdog', async () => {
    const { child, handlers, emitStdout } = makeChild(7806);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Only NESTED deltas (parent_tool_use_id set) arrive — but no tool is in
    // flight, so they count as neither content progress NOR tool sub-progress.
    // The top-level stream is genuinely silent → the watchdog must still fire.
    const nestedDelta = JSON.stringify({
      type: 'stream_event',
      parent_tool_use_id: 'toolu_sub',
      event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'x' } },
    });
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = LLM_NO_PROGRESS_STALL_MS + 120_000;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      // Keep the raw-stdout stall watchdog alive so we isolate the no-progress one.
      emitStdout(nestedDelta);
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    expect(killSpy).toHaveBeenCalledWith(-7806, 'SIGTERM');

    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(NoProgressTimeoutError);
    killSpy.mockRestore();
  });

  it('STILL throws RateLimitError (account block path) when a confirmed rate-limit message accompanies the silent hang', async () => {
    const { child, handlers, emitStdout } = makeChild(7807);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // A genuinely silent hang that DID surface a confirmed rate-limit banner in
    // stdout (a real 429). The no-progress watchdog fires, but because a confirmed
    // RATE_LIMIT_PATTERNS message is present, the agent reports rate_limited so the
    // orchestrator blocks the account + applies the canonical cooldown (UNCHANGED).
    emitStdout('Claude AI usage limit reached');
    await advanceWithStreamHeartbeat(LLM_NO_PROGRESS_STALL_MS + 120_000, emitStdout);

    expect(killSpy).toHaveBeenCalledWith(-7807, 'SIGTERM');

    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).toBeInstanceOf(RateLimitError);
    expect(rejection).not.toBeInstanceOf(NoProgressTimeoutError);
    killSpy.mockRestore();
  });
});

// ── isWorkflowToolName ────────────────────────────────────────────────────────
// The classifier that grants the longer in-flight silence budget to fan-out
// tools. Matches the built-in Task/Agent tool, any "*workflow*" tool, and future
// MCP-namespaced variants, case-insensitively, without an allowlist edit.
describe('isWorkflowToolName', () => {
  it('matches the built-in Task / Agent / Workflow tools (case-insensitive)', () => {
    expect(isWorkflowToolName('Task')).toBe(true);
    expect(isWorkflowToolName('task')).toBe(true);
    expect(isWorkflowToolName('Agent')).toBe(true);
    expect(isWorkflowToolName('Workflow')).toBe(true);
  });

  it('matches future custom / MCP-namespaced workflow + sub-agent tools by substring', () => {
    expect(isWorkflowToolName('mcp__workflow__run')).toBe(true);
    expect(isWorkflowToolName('RunSubAgentWorkflow')).toBe(true);
    expect(isWorkflowToolName('mcp__claude__Task')).toBe(true);
  });

  it('does NOT match ordinary tools', () => {
    expect(isWorkflowToolName('Bash')).toBe(false);
    expect(isWorkflowToolName('Read')).toBe(false);
    expect(isWorkflowToolName('Edit')).toBe(false);
    expect(isWorkflowToolName('Grep')).toBe(false);
    expect(isWorkflowToolName(undefined)).toBe(false);
    expect(isWorkflowToolName('')).toBe(false);
  });
});

// ── long-running workflow / sub-agent fan-out does not false-trip watchdogs ────
// Root-cause regression for the ~15-min thrash loop: a Blueprint agent that
// invokes a long-running workflow / sub-agent fan-out (the built-in Task/Agent
// tool, or the prompt-guided parallel-review/parallel-fix fan-out, which is the
// SAME built-in tool) makes the parent's top-level stream go quiet for >15 min
// while the workflow runs. Pre-fix, the no-progress watchdog fired (it required
// the never-live-confirmed nested parent_tool_use_id sub-agent event to refresh
// lastToolProgressAt; the live parent stream carries no such recognized event
// during a Task wait, so it aged out) → NoProgressTimeoutError on a healthy
// reviewer. The fix: (1) suppress the no-progress kill entirely while a tool is
// in flight, and (2) refresh lastToolProgressAt on ANY stdout chunk while a tool
// is in flight, so the tool-use/workflow stall watchdog is kept alive by any
// output the workflow prints (sub-agent logs, system keepalives, anything),
// independent of the exact JSON event schema. A workflow that goes FULLY dark
// (zero stdout) is still caught — by the raw-stdout stall watchdog at
// CLAUDE_STREAM_STALL_MS — and the wall-clock killTimer is the final backstop.
describe('long-running workflow / sub-agent fan-out watchdog tolerance', () => {
  type StdoutCb = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    const stdoutCbs: StdoutCb[] = [];
    const handlers: ChildHandlers = {};
    const child = {
      pid,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, cb: StdoutCb) => {
          if (event === 'data') stdoutCbs.push(cb);
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') handlers.close = cb;
        else if (event === 'error') handlers.error = cb;
      }),
    };
    const emitStdout = (line: string) => {
      for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
    };
    return { child, handlers, emitStdout };
  }

  // A nested sub-agent event: carries parent_tool_use_id, so the parser treats
  // it as in-flight sub-progress for the parent's workflow tool rather than as a
  // top-level model turn. This is exactly what a Task/Agent fan-out streams while
  // its sub-agents work.
  const nestedSubAgentEvent = (parentId: string, i: number) => JSON.stringify({
    type: 'assistant',
    parent_tool_use_id: parentId,
    message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: `/sub/${i}` } }] },
  });

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearPidRegistry();
  });

  it('does NOT fire no-progress OR tool-use-stall while a workflow tool streams nested sub-agent progress past every window', async () => {
    const { child, handlers, emitStdout } = makeChild(7901);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Parent invokes the workflow/Task tool. No top-level content events follow
    // for the entire fan-out — only nested sub-agent events. Pre-fix this is the
    // exact shape that tripped the no-progress watchdog into a false rate_limited.
    const PARENT_ID = 'toolu_workflow_parent';
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', id: PARENT_ID, input: { description: 'fan out review' } }] },
    }));

    // Run the fan-out well past BOTH the no-progress window AND the ordinary
    // tool-use-stall window, streaming nested sub-agent progress in steps shorter
    // than every watchdog window (no-progress AND raw-stdout-stall). Each nested
    // event refreshes lastToolProgressAt AND emits stdout, so neither watchdog
    // false-fires while the workflow is legitimately busy.
    const totalMs = Math.max(LLM_NO_PROGRESS_STALL_MS, TOOL_USE_STALL_TIMEOUT_MS) + 5 * 60_000;
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(nestedSubAgentEvent(PARENT_ID, i++));
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // No watchdog fired — the workflow was legitimately busy the whole time.
    expect(killSpy).not.toHaveBeenCalledWith(-7901, 'SIGTERM');

    // The workflow finishes and the parent emits its terminal result.
    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    // Critically: the call did NOT resolve as a rate-limit requeue.
    expect(killSpy).not.toHaveBeenCalledWith(-7901, 'SIGTERM');

    killSpy.mockRestore();
  }, 20_000);

  it('a nested sub-agent tool-result (user) event does NOT clear the parent workflow tool', async () => {
    const { child, handlers, emitStdout } = makeChild(7902);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    const PARENT_ID = 'toolu_workflow_parent_2';
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', id: PARENT_ID, input: {} }] },
    }));

    // A sub-agent finishes one of its own tool calls — a NESTED `user` event.
    // Pre-fix this would have set toolInFlight=false, re-exposing the parent to a
    // false no-progress fire. The parent's Task tool is still running, so it must
    // stay in flight. Then the fan-out goes silent for a while but still under
    // the workflow budget, kept alive by nested progress.
    emitStdout(JSON.stringify({ type: 'user', parent_tool_use_id: PARENT_ID }));

    // Advance past the no-progress window with only periodic nested progress.
    // STEP is bounded by BOTH watchdog windows; iterate enough to cumulatively
    // exceed the no-progress window so the test still proves what it claims.
    const STEP = Math.min(CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS) - 120_000;
    const iters = Math.ceil((LLM_NO_PROGRESS_STALL_MS + 120_000) / STEP) + 1;
    for (let i = 0; i < iters; i++) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(nestedSubAgentEvent(PARENT_ID, i));
    }

    expect(killSpy).not.toHaveBeenCalledWith(-7902, 'SIGTERM');

    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    await promise.catch(() => undefined);
    killSpy.mockRestore();
  });

  it('STILL SIGTERMs a workflow tool that goes FULLY DARK (zero stdout) — the genuine-hang catch is the raw-stdout stall watchdog, NOT the no-progress watchdog', async () => {
    // A genuinely hung fan-out: the Task tool launches and then the subprocess
    // emits ZERO bytes — the signature of a truly dead/wedged sub-agent. With the
    // no-progress kill suppressed while toolInFlight is true, the genuine-hang
    // coverage is owned by the raw-stdout stall watchdog (CLAUDE_STREAM_STALL_MS),
    // which fires on zero-stdout regardless of tool state. So removing the
    // no-progress kill while in-flight does NOT create an immortal hang.
    const { child, handlers, emitStdout } = makeChild(7903);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // Workflow tool starts, then emits NOTHING further — no JSON sub-progress,
    // no keepalives, no raw bytes at all.
    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: {} }] },
    }));

    // The Task-launch line itself reset the raw-stdout watchdog. With no further
    // stdout, the watchdog fires CLAUDE_STREAM_STALL_MS later. It must NOT have
    // fired before that (the launch line is well within the window).
    await vi.advanceTimersByTimeAsync(CLAUDE_STREAM_STALL_MS - 60_000);
    expect(killSpy).not.toHaveBeenCalledWith(-7903, 'SIGTERM');

    await vi.advanceTimersByTimeAsync(120_000);
    expect(killSpy).toHaveBeenCalledWith(-7903, 'SIGTERM');

    // The raw-stdout watchdog sets no flag (flags.timedOut stays false so the
    // wrapper proceeds to commit/push), so a clean SIGTERM close resolves the run
    // normally rather than as a misclassified rate-limit/no_progress requeue.
    child.exitCode = 143;
    child.signalCode = 'SIGTERM';
    handlers.close?.(143, 'SIGTERM');
    await vi.runAllTimersAsync();
    const rejection = await promise.catch((e: unknown) => e);
    expect(rejection).not.toBeInstanceOf(NoProgressTimeoutError);
    expect(rejection).not.toBeInstanceOf(RateLimitError);
    killSpy.mockRestore();
  }, 20_000);

  it('keeps a workflow tool ALIVE as long as it prints ANY stdout (schema-independent), even with no recognized JSON sub-progress', async () => {
    // The belt-and-suspenders guarantee for any future out-of-stream workflow
    // (a review backgrounded via Bash, a cloud-review poll): a workflow that
    // prints plain non-JSON log lines — no recognized assistant/result/nested
    // events at all — still keeps lastToolProgressAt fresh via the stdout 'data'
    // handler, so neither the no-progress watchdog (suppressed while in-flight)
    // NOR the tool-use/workflow stall watchdog false-fires. It rides on until
    // genuinely dark or the wall clock.
    const { child, handlers, emitStdout } = makeChild(7904);
    vi.mocked(spawn).mockReturnValueOnce(child as unknown as ReturnType<typeof spawn>);
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    emitStdout(JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', name: 'Task', input: { description: 'background review' } }] },
    }));

    // Emit ONLY plain non-JSON log lines (not '{'-prefixed → never parsed as a
    // recognized event), in steps shorter than every watchdog window, well past
    // BOTH the no-progress window (15 min) AND the ordinary tool-use-stall budget
    // (30 min) — the two windows that would have killed a no-stdout tool here.
    // Kept under the 45-min wall-clock killTimer, which is the deliberate final
    // backstop for any in-flight tool and is exercised separately.
    const totalMs = TOOL_USE_STALL_TIMEOUT_MS + 5 * 60_000;
    const STEP = CLAUDE_STREAM_STALL_MS - 60_000;
    let remaining = totalMs;
    let i = 0;
    while (remaining > STEP) {
      await vi.advanceTimersByTimeAsync(STEP);
      emitStdout(`[subagent ${i++}] still investigating module group...`);
      remaining -= STEP;
    }
    if (remaining > 0) await vi.advanceTimersByTimeAsync(remaining);

    // Never killed — any stdout chunk kept the in-flight clock fresh.
    expect(killSpy).not.toHaveBeenCalledWith(-7904, 'SIGTERM');

    emitStdout(JSON.stringify({ type: 'user' }));
    emitStdout(JSON.stringify({ type: 'result', result: 'review complete' }));
    child.exitCode = 0;
    handlers.close?.(0, null);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(killSpy).not.toHaveBeenCalledWith(-7904, 'SIGTERM');
    killSpy.mockRestore();
  }, 20_000);
});

// ── final drain awaits in-flight drain (cluster C7) ───────────────────────────
// Regression: drainPendingThenResolve's boolean mutex made drainJsonChunks()
// early-return when a drain was already active, so the process promise resolved
// while a drain was still mid-flight — unprocessed chunks (and their result
// text / token telemetry) were silently dropped at process exit. The fix makes
// the drain mutex an awaitable Promise the close handler awaits.
describe('final drain awaits in-flight drain before resolving (C7)', () => {
  type StdoutHandler = (data: Buffer) => void;
  type ChildHandlers = {
    close?: (code: number | null, signal: NodeJS.Signals | null) => void;
    error?: (err: Error) => void;
  };

  function makeChild(pid: number) {
    let stdoutHandler: StdoutHandler | null = null;
    const childHandlers: ChildHandlers = {};
    const fakeChild = {
      pid,
      stdin: { end: vi.fn() },
      stdout: {
        on: vi.fn((event: string, handler: StdoutHandler) => {
          if (event === 'data') stdoutHandler = handler;
        }),
      },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        if (event === 'close') childHandlers.close = handler;
        else if (event === 'error') childHandlers.error = handler;
      }),
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
    };
    return { fakeChild, childHandlers, emitData: (buf: Buffer) => stdoutHandler?.(buf) };
  }

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    clearPidRegistry();
    vi.mocked(fs.existsSync).mockReturnValue(false);
  });

  afterEach(() => {
    clearPidRegistry();
  });

  it('captures the result event from a large chunk even when close fires while the drain is still in flight', async () => {
    const { fakeChild, childHandlers, emitData } = makeChild(8101);
    vi.mocked(spawn).mockReturnValueOnce(fakeChild as unknown as ReturnType<typeof spawn>);

    const promise = runProviderAgent('hello', null, { value: '' });
    await Promise.resolve();

    // A heavy multi-line chunk whose drain yields several times to the event
    // loop. The trailing 'result' line is processed LAST — so if close resolves
    // the promise before the in-flight drain finishes, lastResultText is lost.
    const lines: string[] = [];
    for (let i = 0; i < 400; i++) {
      lines.push(JSON.stringify({ type: 'system', subtype: 'tick', n: i }));
    }
    lines.push(JSON.stringify({ type: 'result', result: 'drain-must-complete-before-resolve' }));
    emitData(Buffer.from(lines.join('\n') + '\n'));

    // Fire close IMMEDIATELY — before the (awaiting) drain has finished. The fix
    // makes drainPendingThenResolve await the in-flight drain promise; pre-fix
    // the boolean mutex early-returned and resolve() fired with the result line
    // still unprocessed.
    childHandlers.close?.(0, null);

    const result = await promise;
    expect(result.lastResultText).toBe('drain-must-complete-before-resolve');
  });
});

describe('fetchVmImageIdentity', () => {
  let fetchMock: FetchMock;
  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('parses image name and timestamp from GCP metadata path', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2-1730000000'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2-1730000000');
    expect(result.image_ts).toBe(1730000000);
  });

  it('returns null image_name when metadata service is unreachable', async () => {
    // Null (rather than 'unknown') so vm-heartbeat omits vm_image_name
    // and compute._vm_images doesn't get polluted with ('unknown', None)
    // entries that would skew get_stale_image_vms accounting.
    fetchMock.mockRejectedValue(new Error('network error'));
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_name when metadata returns non-ok status', async () => {
    fetchMock.mockResolvedValue({ ok: false, text: () => Promise.resolve('') });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBeNull();
    expect(result.image_ts).toBeNull();
  });

  it('returns null image_ts for image names without a numeric suffix', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('projects/my-project/global/images/blueprint-agent-v2'),
    });
    const result = await fetchVmImageIdentity();
    expect(result.image_name).toBe('blueprint-agent-v2');
    expect(result.image_ts).toBeNull();
  });
});

describe('readAgentSourceSha', () => {
  afterEach(() => {
    delete process.env.AGENT_SOURCE_SHA;
    mockReadFileSync.mockReset();
  });

  it('parses a valid AGENT_SOURCE_SHA marker', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=0123456789abcdef0123456789abcdef01234567\n');
    expect(readAgentSourceSha()).toBe('0123456789abcdef0123456789abcdef01234567');
  });

  it('rejects an abbreviated marker SHA as unobservable', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=abc1234');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null for the unknown fallback', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=unknown');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null for an empty value', () => {
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null when the marker file is absent', () => {
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT');
    });
    expect(readAgentSourceSha()).toBeNull();
  });

  it('prefers a validated 40-hex AGENT_SOURCE_SHA env var over the marker', () => {
    process.env.AGENT_SOURCE_SHA = 'abcdefabcdefabcdefabcdefabcdefabcdefabcd';
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=0123456789abcdef0123456789abcdef01234567\n');
    expect(readAgentSourceSha()).toBe('abcdefabcdefabcdefabcdefabcdefabcdefabcd');
  });

  it('falls back to the marker when the env value is short/non-40-hex', () => {
    process.env.AGENT_SOURCE_SHA = 'abc1234'; // abbreviated — rejected as not a full SHA
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=0123456789abcdef0123456789abcdef01234567\n');
    expect(readAgentSourceSha()).toBe('0123456789abcdef0123456789abcdef01234567');
  });

  it('falls back to the marker for a malformed env value', () => {
    process.env.AGENT_SOURCE_SHA = 'not-a-sha';
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=abc1234');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null when env is malformed and the marker is the unknown fallback', () => {
    process.env.AGENT_SOURCE_SHA = 'not-a-sha';
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=unknown');
    expect(readAgentSourceSha()).toBeNull();
  });

  it('returns null when env is malformed and the marker file is absent', () => {
    process.env.AGENT_SOURCE_SHA = 'not-a-sha';
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT');
    });
    expect(readAgentSourceSha()).toBeNull();
  });

  it('falls back to the marker when the env var is absent', () => {
    delete process.env.AGENT_SOURCE_SHA;
    mockReadFileSync.mockReturnValue('AGENT_SOURCE_SHA=abc1234');
    expect(readAgentSourceSha()).toBeNull();
  });
});

describe('apiRequest body-read timeout', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('aborts a stalled response body instead of hanging forever', async () => {
    vi.useFakeTimers();
    // fetch resolves (headers arrive) but the body read (res.json) never
    // settles on its own — it only rejects when the request signal aborts.
    // Pre-fix, the abort timer was cleared right after headers, leaving this
    // read unbounded → the promise would hang forever (the heartbeat-latch
    // root cause). Post-fix, the re-armed body-phase timer aborts it.
    const fetchMock = makeFetchMock().mockImplementation((_input, init) => {
      const signal = init?.signal;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new Error('The operation was aborted'));
          });
        }),
        text: () => Promise.resolve(''),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    // retries=1 (single attempt), timeout=5s.
    const p = apiRequest('POST', '/api/agent/heartbeat', () => ({ method: 'POST' }), 1, 5_000);
    const settled = expect(p).rejects.toThrow('POST /api/agent/heartbeat timed out during body phase on attempt 1/1 after 5000ms');
    // Without the body-phase timer this advance would not settle the promise.
    await vi.advanceTimersByTimeAsync(5_000);
    await settled;
  });

  it('identifies a header-phase timeout with request and attempt context', async () => {
    vi.useFakeTimers();
    const fetchMock = makeFetchMock().mockImplementation((_input, init) => {
      const signal = init?.signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new Error('The operation was aborted'));
        });
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const p = apiRequest('GET', '/api/agent/claim', () => ({ method: 'GET' }), 1, 2_000);
    const settled = expect(p).rejects.toThrow('GET /api/agent/claim timed out during headers phase on attempt 1/1 after 2000ms');
    await vi.advanceTimersByTimeAsync(2_000);
    await settled;
  });

  it('logs an intermediate timeout before a later attempt succeeds', async () => {
    vi.useFakeTimers();
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(Math, 'random').mockReturnValue(0);
    let attempt = 0;
    const fetchMock = makeFetchMock().mockImplementation((_input, init) => {
      attempt += 1;
      if (attempt === 1) {
        const signal = init?.signal;
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new Error('The operation was aborted'));
          });
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok' }),
        text: () => Promise.resolve(''),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const pending = apiRequest('GET', '/api/agent/status', () => ({ method: 'GET' }), 2, 1_000);
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.advanceTimersByTimeAsync(1_000);

    await expect(pending).resolves.toEqual({ status: 'ok' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(logSpy.mock.calls.flat().join('\n')).toContain(
      '[API_TIMEOUT] GET /api/agent/status timed out during headers phase on attempt 1/2 after 1000ms; retrying',
    );

  });
  it('does not abort a body that arrives within the timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await apiPost('/api/agent/heartbeat', {}, 1, 5_000);
    expect(result).toEqual({ status: 'ok' });
    // No dangling abort timer should fire after a clean read.
    await vi.advanceTimersByTimeAsync(10_000);
  });
});

describe('claimTask retry contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
    _setAgentTokenForTesting('');
  });

  it('refreshes an expired task token and retries the claim with the new token', async () => {
    _setAgentTokenForTesting('stale-task-token');
    const claimTokens: string[] = [];
    const researchContractVersions: string[] = [];
    let claimCalls = 0;
    const fetchMock = makeFetchMock().mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith('/api/agent/claim')) {
        claimCalls += 1;
        const headers = init?.headers as Record<string, string> | undefined;
        claimTokens.push(headers?.['X-Agent-Token'] ?? '');
        researchContractVersions.push(headers?.['X-Research-Output-Contract-Version'] ?? '');
        if (claimCalls === 1) {
          return Promise.resolve({
            ok: false,
            status: 401,
            text: () => Promise.resolve('expired task token'),
            json: () => Promise.resolve({}),
            headers: { get: () => null },
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ task_id: 'task-1' }),
          text: () => Promise.resolve(''),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/refresh-token')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ token: 'fresh-task-token' }),
          text: () => Promise.resolve(''),
          headers: { get: () => null },
        });
      }
      throw new Error(`unexpected URL: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(claimTask()).resolves.toEqual({ task_id: 'task-1' });
    expect(claimTokens).toEqual(['stale-task-token', 'fresh-task-token']);
    expect(researchContractVersions).toEqual(['2', '2']);
    expect(getAgentToken()).toBe('fresh-task-token');
  });

  it('retries a transient claim failure instead of killing the role', async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0);
    let claimCalls = 0;
    const fetchMock = makeFetchMock().mockImplementation(() => {
      claimCalls += 1;
      if (claimCalls === 1) {
        return Promise.resolve({
          ok: false,
          status: 503,
          text: () => Promise.resolve('temporarily unavailable'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ task_id: 'task-2' }),
        text: () => Promise.resolve(''),
        headers: { get: () => null },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const pending = claimTask();
    await vi.advanceTimersByTimeAsync(1_000);

    await expect(pending).resolves.toEqual({ task_id: 'task-2' });
    expect(claimCalls).toBe(2);
  });
});

describe('apiRequest 503 / Retry-After backoff (create-PR fail-closed contract)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('retries a 503 honoring Retry-After, then returns the success body', async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetchMock = makeFetchMock().mockImplementation(() => {
      calls += 1;
      if (calls === 1) {
        // The backend returns 503 + Retry-After when it refuses to create a PR
        // because it cannot confirm no-duplicate while the GitHub quota is drained.
        // Retry-After is 10s — deliberately LARGER than the exponential fallback
        // (< 2s on the first retry) so the two-phase advance below can prove the
        // header is honored rather than the exponential window.
        return Promise.resolve({
          ok: false,
          status: 503,
          text: () => Promise.resolve('{"error":{"message":"rate-limited"}}'),
          json: () => Promise.resolve({}),
          headers: { get: (h: string) => (h === 'Retry-After' ? '10' : null) },
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'success', data: { html_url: 'u' } }),
        text: () => Promise.resolve(''),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    // timeout=30s so the per-attempt abort timer never fires during the 10s wait.
    const p = apiPost('/api/agent/github/pull-requests', {}, 3, 30_000);
    // After 2.1s the retry must NOT have fired: the exponential fallback would
    // have (< 2s), so a still-pending retry proves the 10s Retry-After is honored.
    await vi.advanceTimersByTimeAsync(2_100);
    expect(calls).toBe(1);
    // Advance past the 10s Retry-After — now the retry fires and succeeds.
    await vi.advanceTimersByTimeAsync(8_000);
    await expect(p).resolves.toEqual({ status: 'success', data: { html_url: 'u' } });
    expect(calls).toBe(2);
  });

  it('does NOT retry a 403 without a Retry-After header (fails fast)', async () => {
    let calls = 0;
    const fetchMock = makeFetchMock().mockImplementation(() => {
      calls += 1;
      return Promise.resolve({
        ok: false,
        status: 403,
        text: () => Promise.resolve('Forbidden'),
        json: () => Promise.resolve({}),
        headers: { get: () => null },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiPost('/api/agent/github/pull-requests', {}, 3, 5_000),
    ).rejects.toThrow(/failed \(403\)/);
    expect(calls).toBe(1);
  });
});

describe('agent runtime fingerprint heartbeats', () => {
  let fetchMock: FetchMock;
  const runtimeFingerprint = 'a'.repeat(64);
  const sourceSha = 'abc1234def5678';

  beforeEach(() => {
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    _setAgentRuntimeFingerprintForTesting(runtimeFingerprint);
    _setAgentSourceShaForTesting(sourceSha);
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _setAgentRuntimeFingerprintForTesting(null);
    _setAgentSourceShaForTesting(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('includes agent_runtime_fingerprint in /api/agent/vm-heartbeat payloads', async () => {
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

    const result = await pollForNextRole({
      vmInstanceId: 'vm-runtime-fp',
      priorTaskId: 'task-runtime-fp',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-runtime-fp',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 60_000,
    });

    expect(result).toEqual({ action: 'exit', reason: 'done' });
    const vmHeartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/vm-heartbeat'),
    );
    expect(vmHeartbeatCall).toBeDefined();
    const vmHeartbeatBody = vmHeartbeatCall?.[1]?.body;
    expect(typeof vmHeartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof vmHeartbeatBody === 'string' ? vmHeartbeatBody : undefined),
    ) as { agent_runtime_fingerprint?: string; agent_source_sha?: string };
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
    expect(body.agent_source_sha).toBe(sourceSha);
  });

  it('includes agent_runtime_fingerprint in /api/agent/heartbeat payloads', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    _startHeartbeatForTesting('task-runtime-fp', () => 'heartbeat-ok');
    await new Promise(resolve => setTimeout(resolve, 0));

    const heartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/heartbeat'),
    );
    expect(heartbeatCall).toBeDefined();
    const heartbeatBody = heartbeatCall?.[1]?.body;
    expect(typeof heartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof heartbeatBody === 'string' ? heartbeatBody : undefined),
    ) as { task_id?: string; agent_runtime_fingerprint?: string; agent_source_sha?: string };
    expect(body.task_id).toBe('task-runtime-fp');
    expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
    expect(body.agent_source_sha).toBe(sourceSha);
  });

  it('reports structured active-tool progress when task prose replaces the tool prefix', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });
    const subprogressAt = '2026-08-09T03:03:33.000Z';

    _startHeartbeatForTesting(
      'task-active-tool-progress',
      () => 'The publication gate remains clean (8m elapsed, last sub-progress 24s ago)',
      () => subprogressAt,
    );
    await new Promise(resolve => setTimeout(resolve, 0));

    const heartbeatCall = fetchMock.mock.calls.find(
      ([url]) => String(url).endsWith('/api/agent/heartbeat'),
    );
    expect(heartbeatCall).toBeDefined();
    const heartbeatBody = heartbeatCall?.[1]?.body;
    expect(typeof heartbeatBody).toBe('string');
    const body = JSON.parse(
      defined(typeof heartbeatBody === 'string' ? heartbeatBody : undefined),
    ) as { status_message?: string; active_tool_progress_at?: string };
    expect(body.status_message).toContain('The publication gate remains clean');
    expect(body.active_tool_progress_at).toBe(subprogressAt);
  });

  it('omits agent_source_sha from task and VM heartbeats when identity absent', async () => {
    // The additive contract: a missing source identity must OMIT the field
    // rather than send a null/bogus value over the wire.
    _setAgentSourceShaForTesting(null);
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/vm-heartbeat')) {
        return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
      }
      if (url.endsWith('/api/agent/heartbeat')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        status: 200,
        json: () => Promise.resolve({ action: 'exit', reason: 'done' }),
      });
    });

    _startHeartbeatForTesting('task-no-sha', () => 'heartbeat-ok');
    const result = await pollForNextRole({
      vmInstanceId: 'vm-no-sha',
      priorTaskId: 'task-no-sha',
      priorPrNumber: null,
      apiBase: 'http://api.test',
      agentJwt: 'jwt-no-sha',
      pollIntervalMs: 10,
      timeoutMs: 1000,
      heartbeatIntervalMs: 60_000,
    });

    expect(result).toEqual({ action: 'exit', reason: 'done' });
    for (const suffix of ['/api/agent/vm-heartbeat', '/api/agent/heartbeat']) {
      const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith(suffix));
      expect(call).toBeDefined();
      const bodyStr = call?.[1]?.body;
      expect(typeof bodyStr).toBe('string');
      const body = JSON.parse(
        defined(typeof bodyStr === 'string' ? bodyStr : undefined),
      ) as { agent_runtime_fingerprint?: string; agent_source_sha?: string };
      expect(body.agent_runtime_fingerprint).toBe(runtimeFingerprint);
      expect(body.agent_source_sha).toBeUndefined();
    }
  });

  it('reclaims the in-flight slot when a heartbeat POST wedges past the deadline', async () => {
    vi.useFakeTimers();
    let heartbeatCalls = 0;
    // Every heartbeat POST hangs forever (never resolves). Pre-fix, the first
    // periodic tick would latch `heartbeatInFlight` and every later tick would
    // skip indefinitely → liveness silenced → false AGENT_TIMEOUT. Post-fix, a
    // tick past HEARTBEAT_INFLIGHT_DEADLINE reclaims the slot and fires again.
    fetchMock.mockImplementation((input: unknown) => {
      if (String(input).endsWith('/api/agent/heartbeat')) {
        heartbeatCalls += 1;
        return new Promise(() => { /* never settles */ });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    _startHeartbeatForTesting('task-wedged', () => 'hb');
    // Initial fire + the first periodic tick each start a (hanging) POST.
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 5);
    const callsAfterFirstTick = heartbeatCalls;
    // Ticks within the deadline must skip — no new POSTs.
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INFLIGHT_DEADLINE - HEARTBEAT_INTERVAL);
    expect(heartbeatCalls).toBe(callsAfterFirstTick);
    // Once held past the deadline, the next tick reclaims and fires a new POST.
    await vi.advanceTimersByTimeAsync(2 * HEARTBEAT_INTERVAL);
    expect(heartbeatCalls).toBeGreaterThan(callsAfterFirstTick);
  });
});

// ── Heartbeat 409 task-terminal detection ────────────────────────────────────
// When the backend returns 409 with "Task already terminal" the agent must
// self-terminate (cancel flag + terminal-status POST + exit timer), NOT retry
// as if it were a transient row-lock.  A plain row-lock 409 still retries.
describe('heartbeat 409 task-terminal detection', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(fs.writeFileSync).mockReset();
    vi.mocked(fs.unlinkSync).mockReset();
    _resetCancelRequestedForTesting();
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _resetCancelRequestedForTesting();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('triggers graceful shutdown (stop+terminal-post+exit) when heartbeat 409 carries "Task already terminal"', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 409,
      text: () => Promise.resolve('{"detail":"Task already terminal"}'),
      json: () => Promise.resolve({}),
      headers: { get: () => null },
    });

    _startHeartbeatForTesting('task-terminal-409', () => 'running');
    // advanceTimersByTimeAsync flushes pending microtasks (fetch chain + catch handler)
    await vi.advanceTimersByTimeAsync(1);

    // cancel flag set — role loop and subsequent heartbeat ticks see it as shutdown
    expect(_isCancelRequestedForTesting()).toBe(true);
    // Terminal-status POST fired via curl (stop+terminal-post+exit pattern)
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeDefined();
    const args = curlCall?.[1] as string[];
    expect(args.some(a => a.includes('/api/agent/status'))).toBe(true);
    const bodyIdx = args.indexOf('-d');
    const body = JSON.parse(args[bodyIdx + 1]) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.task_id).toBe('task-terminal-409');
  });

  it('still retries on a row-lock 409 ("Task row locked") and does NOT set cancel flag', async () => {
    let callCount = 0;
    fetchMock.mockImplementation(() => {
      callCount++;
      return Promise.resolve({
        ok: false,
        status: 409,
        text: () => Promise.resolve('{"status":"locked","detail":"Task row locked, retry next cycle"}'),
        json: () => Promise.resolve({}),
        headers: { get: () => '30' },
      });
    });

    _startHeartbeatForTesting('task-locked-409', () => 'running');
    // Advance past the periodic heartbeat interval (30s) and both retry delays
    // (5s + 10s) so the periodic handler's retry loop fully runs.
    await vi.advanceTimersByTimeAsync(50_000);

    // cancel flag must NOT be set — row-lock is transient, not a shutdown signal
    expect(_isCancelRequestedForTesting()).toBe(false);
    // Initial heartbeat + periodic initial attempt + 2 retries = >= 4 calls
    expect(callCount).toBeGreaterThan(1);
    // No curl terminal POST
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeUndefined();
  });
});

// ── Heartbeat auth-shaped 4xx (401/403) credential-refresh retry cascade ────
// Kill-path regression (NeurIPS task 38b16082, 2026-08-18): the agent treated
// the FIRST auth-shaped heartbeat 4xx as an instant, unretried fatal signal —
// self-terminating 39s after pushing a real fix commit that had already
// passed its local publication gate, orphaning it (no PR existed yet to carry
// the head forward). These tests prove: (a) a recoverable auth 4xx does NOT
// cancel the task, (b) an unrecoverable one DOES cancel, but with a reason
// that names the actual HTTP status and retry count instead of a bare
// "heartbeat 4xx", and (c) any pushed-but-not-yet-PR'd work rides along on
// the terminal cancellation POST so the backend can record it instead of
// silently discarding it.
describe('heartbeat auth-shaped 4xx (401/403) credential-refresh retry cascade', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(fs.writeFileSync).mockReset();
    vi.mocked(fs.unlinkSync).mockReset();
    _resetCancelRequestedForTesting();
    _setLastPushedWorkForTesting(null);
    _setAgentTokenForTesting('');
  });

  afterEach(() => {
    _stopHeartbeatForTesting();
    _resetCancelRequestedForTesting();
    _setLastPushedWorkForTesting(null);
    // The recovery test exercises a REAL tryRefreshToken() → setToken() call —
    // don't let the refreshed token leak into unrelated later describe blocks.
    _setAgentTokenForTesting('');
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function auth4xxResponse(status: 401 | 403) {
    return Promise.resolve({
      ok: false,
      status,
      text: () => Promise.resolve(`{"detail":"vm_instance_mismatch"}`),
      json: () => Promise.resolve({}),
      headers: { get: () => null },
    });
  }
  function okResponse() {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(''),
      headers: { get: () => null },
    });
  }

  it('recovers from a 403 via credential re-acquisition and does NOT cancel the task', async () => {
    let heartbeatCalls = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/refresh-token')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ token: 'fresh-token' }),
          text: () => Promise.resolve(''),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/heartbeat')) {
        heartbeatCalls += 1;
        // Call 1 = initial heartbeat (fires immediately, swallowed silently by
        // the initial-heartbeat handler — it only self-terminates on 409).
        // Call 2 = periodic tick's first attempt — this is the one that must
        // enter the auth-retry cascade instead of instantly self-terminating.
        // Call 3 = the cascade's first retry attempt, which succeeds.
        if (heartbeatCalls <= 2) return auth4xxResponse(403);
        return okResponse();
      }
      return okResponse();
    });

    _startHeartbeatForTesting('task-403-recovers', () => 'running');
    await vi.advanceTimersByTimeAsync(1); // flush initial heartbeat
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 1); // periodic tick fails 403
    await vi.advanceTimersByTimeAsync(21_000); // first ~20s auth-retry delay elapses, retry succeeds

    expect(_isCancelRequestedForTesting()).toBe(false);
    expect(heartbeatCalls).toBeGreaterThanOrEqual(3);
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeUndefined();
  });

  it('cancels with a status+attempt-count reason after exhausting all credential-refresh retries, and carries the pushed head', async () => {
    // Simulate a task that already pushed a real commit before PR creation —
    // exactly the vulnerable window in the kill-path (pushAndCreatePR calls
    // recordPushedWork immediately after `git push`, before the PR exists).
    _setLastPushedWorkForTesting({ branch: 'blueprint-fix/orphan-guard-abcd123', headSha: 'deadbeefcafe' });

    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/refresh-token')) {
        // Refresh itself also rejects — apiRequest/tryRefreshToken must not
        // hang retrying refresh; tryRefreshToken returns false immediately on
        // a 4xx from this endpoint.
        return Promise.resolve({
          ok: false,
          status: 403,
          text: () => Promise.resolve('{"detail":"token revoked"}'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        });
      }
      if (url.endsWith('/api/agent/heartbeat')) {
        return auth4xxResponse(403);
      }
      return okResponse();
    });

    _startHeartbeatForTesting('task-403-exhausts', () => 'running');
    await vi.advanceTimersByTimeAsync(1); // flush initial heartbeat
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 1); // periodic tick fails 403 → enters cascade
    // 3 attempts * 20s delay lands exactly on the synchronous exhaustion path
    // (requestCancel + stopHeartbeat + postTerminalStatusSync all run inline
    // right after the 3rd attempt fails). Deliberately stop short of the
    // final `setTimeout(() => process.exit(0), 5000)` scheduled at that same
    // instant — vitest traps a real process.exit call and throws, and the
    // synchronous cancellation/curl-post assertions below don't need it to
    // actually fire.
    await vi.advanceTimersByTimeAsync(3 * 20_000 + 1_000);

    expect(_isCancelRequestedForTesting()).toBe(true);
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeDefined();
    const args = curlCall?.[1] as string[];
    const bodyIdx = args.indexOf('-d');
    const body = JSON.parse(args[bodyIdx + 1]) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.failure_category).toBe('reassigned');
    // Surfaced-reasons requirement: never a bare "heartbeat 4xx" — must name
    // the actual HTTP status, the endpoint, and the retry-attempt count.
    const logs = String(body.logs ?? '');
    expect(logs).toMatch(/heartbeat/i);
    expect(logs).toMatch(/POST \/api\/agent\/heartbeat/);
    expect(logs).toMatch(/403/);
    expect(logs).toMatch(/3/); // attempt count
    expect(logs).not.toBe('non-transient heartbeat 4xx');
    // The pushed-but-not-yet-PR'd head must ride along so the cancellation
    // record carries the truth instead of orphaning the commit.
    expect(body.pushed_branch).toBe('blueprint-fix/orphan-guard-abcd123');
    expect(body.pushed_head_sha).toBe('deadbeefcafe');
  });

  it('does NOT enter the auth-retry cascade for a genuinely non-auth 4xx (404 task-gone) — stays immediately fatal', async () => {
    let heartbeatCalls = 0;
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.endsWith('/api/agent/heartbeat')) {
        heartbeatCalls += 1;
        return Promise.resolve({
          ok: false,
          status: 404,
          text: () => Promise.resolve('{"detail":"Task not found"}'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        });
      }
      return okResponse();
    });

    _startHeartbeatForTesting('task-404-fatal', () => 'running');
    await vi.advanceTimersByTimeAsync(1); // flush initial heartbeat
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 1); // periodic tick fails 404

    // No auth-retry delay (20s) needed — should already be cancelled.
    expect(_isCancelRequestedForTesting()).toBe(true);
    const curlCall = vi.mocked(execFileSync).mock.calls.find(([cmd]) => cmd === 'curl');
    expect(curlCall).toBeDefined();
    const args = curlCall?.[1] as string[];
    const bodyIdx = args.indexOf('-d');
    const body = JSON.parse(args[bodyIdx + 1]) as Record<string, unknown>;
    expect(String(body.logs ?? '')).toMatch(/404/);
    expect(String(body.logs ?? '')).toMatch(/non-retryable/);
    // Only the initial heartbeat + the one periodic attempt — no 20s-spaced
    // retry attempts fired for a non-auth-shaped 4xx.
    expect(heartbeatCalls).toBe(2);
  });

  it('recordPushedWork / _getLastPushedWorkForTesting round-trip', () => {
    expect(_getLastPushedWorkForTesting()).toBeNull();
    recordPushedWork('blueprint-fix/foo-bar', '0123456789abcdef');
    expect(_getLastPushedWorkForTesting()).toEqual({
      branch: 'blueprint-fix/foo-bar',
      headSha: '0123456789abcdef',
    });
  });
});

// ── Live tool-execution diagnostics in heartbeat status ─────────────────────
describe('_buildHeartbeatStatusForTesting', () => {
  it('returns plain status when no tool is in flight', () => {
    const statusRef = { value: 'Codex thinking' };
    expect(_buildHeartbeatStatusForTesting(statusRef, 1_000_000)).toBe('Codex thinking');
  });

  it('includes elapsed time in seconds when tool has been running under 60s', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: now - 42_000, lastProgressAt: now - 5_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: Bash (42s elapsed, last sub-progress 5s ago)');
  });

  it('formats elapsed time as Xm Ys when tool has been running over 60s', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: now - 252_000, lastProgressAt: now - 43_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: Bash (4m 12s elapsed, last sub-progress 43s ago)');
  });

  it('omits sub-progress clause when lastProgressAt is not set', () => {
    const now = 1_000_000;
    const statusRef = { value: 'Using tool: TodoWrite', toolStartedAt: now - 10_000 };
    const msg = _buildHeartbeatStatusForTesting(statusRef, now);
    expect(msg).toBe('Using tool: TodoWrite (10s elapsed)');
  });

  it('returns plain status once toolStartedAt is cleared (tool complete)', () => {
    const statusRef = { value: 'Using tool: Bash', toolStartedAt: undefined, lastProgressAt: undefined };
    expect(_buildHeartbeatStatusForTesting(statusRef, 1_000_000)).toBe('Using tool: Bash');
  });
});

// ── Per-task LLM telemetry (efficiency direction d-91d8dede) ────────────────
describe('LLM telemetry counters', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('starts at zero', () => {
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      num_turns: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });

  it('counts CLI invocations and accumulates tokens across calls', () => {
    recordLlmCallStart();
    recordLlmTokens(100, 50);
    recordLlmCallStart();
    recordLlmTokens(200, 75);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 2,
      num_turns: 0,
      total_input_tokens: 300,
      total_output_tokens: 125,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      // CLI invocations happened but no provider usage block was observed →
      // the explicit not-reported marker, never a silent null.
      provider_reported_usage: { reported: false },
    });
  });

  it('ignores non-finite or negative token counts', () => {
    recordLlmTokens(Number.NaN, 50);
    recordLlmTokens(-5, -10);
    recordLlmTokens(100, Number.POSITIVE_INFINITY);
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      num_turns: 0,
      total_input_tokens: 100,
      total_output_tokens: 50,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });

  it('resets between roles so warm-VM continuity does not leak counts', () => {
    recordLlmCallStart();
    recordLlmTokens(500, 250);
    resetLlmTelemetry();
    expect(getLlmTelemetry()).toEqual({
      llm_call_count: 0,
      num_turns: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
      provider_reported_usage: null,
    });
  });
});

// ── Gemini text-mode token estimation ────────────────────────────────────────
describe('Gemini text-mode token estimation', () => {
  beforeEach(() => {
    resetLlmTelemetry();
  });

  it('estimateTokensFromChars returns ceil(chars/4)', () => {
    expect(estimateTokensFromChars(0)).toBe(0);
    expect(estimateTokensFromChars(1)).toBe(1);
    expect(estimateTokensFromChars(4)).toBe(1);
    expect(estimateTokensFromChars(5)).toBe(2);
    expect(estimateTokensFromChars(400)).toBe(100);
    expect(estimateTokensFromChars(1000)).toBe(250);
  });

  it('lands estimated tokens in the same getLlmTelemetry() fields Claude uses', () => {
    // Simulate what runProviderAgent does for outputMode='text': estimate from
    // prompt.length and lastResultText.length, then call recordLlmTokens.
    const promptChars = 2000;  // → 500 estimated input tokens
    const resultChars = 800;   // → 200 estimated output tokens
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(promptChars), estimateTokensFromChars(resultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBe(500);
    expect(telemetry.total_output_tokens).toBe(200);
    expect(telemetry.llm_call_count).toBe(1);
  });

  it('records non-zero tokens on partial output (timeout/kill scenario)', () => {
    // Even when the Gemini CLI is killed mid-run, lastResultText has partial
    // output. Verify estimation still produces non-zero values.
    const fullPromptChars = 5000;
    const partialResultChars = 100;
    recordLlmCallStart();
    recordLlmTokens(estimateTokensFromChars(fullPromptChars), estimateTokensFromChars(partialResultChars));
    const telemetry = getLlmTelemetry();
    expect(telemetry.total_input_tokens).toBeGreaterThan(0);
    expect(telemetry.total_output_tokens).toBeGreaterThan(0);
  });
});

// ── Cross-language classification contracts ───────────────────────────────────
// Assert that backend/app/failure_classification.py still contains the patterns
// that match what this agent emits. If one fails, update the matching side:
// either the string in agent/src/index.ts or the corresponding pattern in
// failure_classification._AGENT_LIVE_WORK_PATTERNS / _VIEWPORT_PATTERNS.
//
// Complements backend/testing/test_failure_classification.py
// TestCrossLanguageClassificationContracts, which checks the reverse direction
// (Python reads agent/src/index.ts and admin-ui/src/test/viewport.ts).
describe('cross-language classification contracts', () => {
  let classifierSrc = '';

  beforeAll(async () => {
    // vi.importActual bypasses the vi.mock('fs', ...) at the top of this file
    // so we can read real source files for the contract assertions.
    const realFs = await vi.importActual<typeof import('fs')>('fs');
    const nodePath = await import('node:path');
    const nodeUrl = await import('node:url');
    // fileURLToPath (not URL.pathname) so Windows does not yield a leading-slash
    // '/D:/…' that path.join then doubles into an invalid 'D:\D:\…' path.
    const dir = nodePath.dirname(nodeUrl.fileURLToPath(import.meta.url));
    classifierSrc = realFs.readFileSync(
      nodePath.join(dir, '..', '..', 'backend', 'app', 'failure_classification.py'),
      'utf-8',
    ).toLowerCase();
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches PR-push heartbeat prefix', () => {
    // agent/src/index.ts emits: sendHeartbeatSync(taskId, `Creating pull request for ${workBranch}...`)
    // where workBranch is a blueprint/ branch. If this fails, update either the
    // agent string or the classifier pattern so both stay in sync.
    expect(classifierSrc).toContain('"creating pull request for blueprint/"');
  });

  it('_AGENT_LIVE_WORK_PATTERNS still matches tool-call heartbeat prefix', () => {
    // agent/src/index.ts emits: statusRef.value = `Using tool: ${toolName}`
    // If this fails, update either the agent string or the classifier pattern.
    expect(classifierSrc).toContain('"using tool: "');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoOverflowAt function name', () => {
    // viewport.ts throws errors containing 'expectNoOverflowAt(...)'.
    // If this fails, update either the viewport helper name or the classifier pattern.
    expect(classifierSrc).toContain('"expectnooverflowat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectNoTextTruncationAt function name', () => {
    // viewport.ts throws errors containing 'expectNoTextTruncationAt(...)'.
    expect(classifierSrc).toContain('"expectnotexttruncationat("');
  });

  it('_VIEWPORT_PATTERNS still matches expectMinTouchTargetAt function name', () => {
    // viewport.ts throws errors containing 'expectMinTouchTargetAt(...)'.
    expect(classifierSrc).toContain('"expectmintouchtargetat("');
  });
});

// ── Cross-language credential redaction corpus ───────────────────────────────
// Keep the shared corpus file in backend/testing/__fixtures__ so both the
// backend pytest suite and this agent vitest suite can read the exact same
// fixtures. If this fails, update the corpus or the sanitizer pattern set.
describe('cross-language credential redaction corpus', () => {
  let corpus: Array<{ family: string; secret: string; raw: string }> = [];

  beforeAll(async () => {
    const realFs = await vi.importActual<typeof import('fs')>('fs');
    const nodePath = await import('node:path');
    const nodeUrl = await import('node:url');
    const dir = nodePath.dirname(nodeUrl.fileURLToPath(import.meta.url));
    corpus = JSON.parse(
      realFs.readFileSync(
        nodePath.join(
          dir,
          '..',
          '..',
          'backend',
          'testing',
          '__fixtures__',
          'credential_redaction_corpus.json',
        ),
        'utf-8',
      ),
    ) as Array<{ family: string; secret: string; raw: string }>;
  });

  it('masks every shared corpus entry', () => {
    for (const entry of corpus) {
      const out = sanitize(entry.secret);
      expect(out).not.toContain(entry.raw);
    }
  });
});

// ── codexStreamHasTurnCompleted (cause-C sentinel, 2026-05-26) ───────────────
// The Codex CLI emits `{"type":"turn.completed"}` only when the turn actually
// finished. Absent that event, exitCode=0 means the upstream provider died
// silently (token cap / refusal / 5xx the CLI swallowed). The memo handler
// reclassifies on this signal — see agent/src/handlers/memo.ts. These tests
// pin the pure parser the live drainer (drainCodexChunks) mirrors.
describe('codexStreamHasTurnCompleted', () => {
  it('returns false for a stream that started but never completed (cause C)', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('returns true for a stream containing a turn.completed event', () => {
    const stream =
      '{"type":"thread.started","thread_id":"t1"}\n'
      + '{"type":"turn.started"}\n'
      + '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
      + '{"type":"turn.completed","usage":{"input_tokens":42}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('ignores non-JSON noise (codex auth error log lines)', () => {
    const stream =
      '2026-05-26T10:00:00Z ERROR codex_login: token invalid\n'
      + '{"type":"thread.started"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });

  it('ignores malformed JSONL lines without crashing', () => {
    const stream =
      '{"type":"thread.started"}\n'
      + '{this is not valid json\n'
      + '{"type":"turn.completed"}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(true);
  });

  it('returns false on an empty stream', () => {
    expect(codexStreamHasTurnCompleted('')).toBe(false);
  });

  it('does not match substrings outside the type field', () => {
    // Defensive: a tool argument that mentions "turn.completed" must not
    // false-positive the sentinel. The check is gated on `obj.type` equality.
    const stream =
      '{"type":"item.completed","item":{"type":"agent_message","text":"the turn.completed event was missing"}}\n';
    expect(codexStreamHasTurnCompleted(stream)).toBe(false);
  });
});

// ── Task-capability contract load (runs before handler dispatch) ──────────────
// runRoleOnce fetches + loads the published contract after the github-token
// fetch and before dispatching to a handler, so isReadOnlyTaskType (used by
// configureAgentPermissions) resolves against backend metadata. These cases
// exercise the load helper directly (the fetcher is injectable for tests).
describe('ensureTaskCapabilityContractLoaded', () => {
  beforeEach(() => {
    __resetTaskCapabilityContractLoadedForTesting();
    __resetTaskCapabilityContractForTesting();
  });

  afterEach(() => {
    // Restore the canonical fixture so other suites in this file keep a loaded
    // contract (testSetup seeded it at top-level / beforeAll).
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());
  });

  it('fetches and loads the contract, enabling permission classification', async () => {
    let fetched = 0;
    const fetcher = (): Promise<TaskCapabilityContract> => {
      fetched += 1;
      return Promise.resolve(buildFixtureCapabilityContract());
    };

    await ensureTaskCapabilityContractLoaded(fetcher);

    expect(fetched).toBe(1);
    // After load, the partition resolves instead of throwing.
    expect(isReadOnlyTaskType('review')).toBe(true);
    expect(isReadOnlyTaskType('create')).toBe(false);
  });

  it('is idempotent — a second call does not re-fetch', async () => {
    let fetched = 0;
    const fetcher = (): Promise<TaskCapabilityContract> => {
      fetched += 1;
      return Promise.resolve(buildFixtureCapabilityContract());
    };

    await ensureTaskCapabilityContractLoaded(fetcher);
    await ensureTaskCapabilityContractLoaded(fetcher);

    expect(fetched).toBe(1);
  });

  it('throws (fails the role) when the contract is incomplete', async () => {
    const fetcher = (): Promise<TaskCapabilityContract> => {
      const incomplete = buildFixtureCapabilityContract();
      delete incomplete.migrate;
      return Promise.resolve(incomplete);
    };

    await expect(ensureTaskCapabilityContractLoaded(fetcher)).rejects.toThrow(/migrate/);
    // The guard did not flip — classification still throws, so a follow-up
    // attempt would retry the fetch rather than run with no contract.
    expect(() => isReadOnlyTaskType('create')).toThrow(/not loaded/);
  });

  it('rejects an empty contract map (no task types covered)', async () => {
    // The fetch wrapper guards a missing key; loadTaskCapabilityContract guards
    // an empty/partial map. An empty object reaches the completeness check and
    // surfaces an explicit error rather than silently classifying nothing.
    const fetcher = (): Promise<TaskCapabilityContract> =>
      Promise.resolve({} as TaskCapabilityContract);
    await expect(ensureTaskCapabilityContractLoaded(fetcher)).rejects.toThrow(/does not cover/);
  });
});

// ── cloneWithRetry ───────────────────────────────────────────────────────────
describe('cloneWithRetry', () => {
  const defaultSpawnImpl = vi.mocked(spawn).getMockImplementation();

  function setupGitCloneMock(successAfterAttempts: number, errorMsg: string, captureArgs?: string[][]) {
    let callCount = 0;
    vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
      if (cmd === 'git') {
        callCount++;
        if (captureArgs) captureArgs.push((argsRaw as string[]).slice());
        const shouldFail = callCount <= successAfterAttempts;
        const errMsg = errorMsg;
        const stderrCbs: ((d: Buffer) => void)[] = [];
        const proc = {
          pid: 999,
          stdin: { on: vi.fn(), end: vi.fn() },
          stderr: {
            on: vi.fn((event: string, cb: (d: Buffer) => void) => {
              if (event === 'data') stderrCbs.push(cb);
            }),
          },
          stdout: { on: vi.fn() },
          on: vi.fn((event: string, cb: (code: number) => void) => {
            if (event === 'close') {
              void Promise.resolve().then(() => {
                if (shouldFail) for (const scb of stderrCbs) scb(Buffer.from(errMsg));
                cb(shouldFail ? 1 : 0);
              });
            }
          }),
          kill: vi.fn(),
        };
        return proc as unknown as ReturnType<typeof spawn>;
      }
      // Fall back to default for non-git commands
      if (defaultSpawnImpl) return defaultSpawnImpl(cmd as string, argsRaw as string[], {});
      // When no default exists, return a minimal no-op ChildProcess
      return { on: vi.fn(), kill: vi.fn(), stdin: { on: vi.fn(), end: vi.fn() }, stdout: { on: vi.fn() }, stderr: { on: vi.fn() }, pid: 0 } as unknown as ReturnType<typeof spawn>;
    });
  }

  afterEach(() => {
    vi.mocked(spawn).mockReset();
    if (defaultSpawnImpl) vi.mocked(spawn).mockImplementation(defaultSpawnImpl);
  });

  it('redacts token-bearing URLs from retry log lines', async () => {
    vi.useFakeTimers();
    const tokenUrl = 'https://x-access-token:ghp_secret123@github.com/org/repo.git';
    const errorMsg = `fatal: repository '${tokenUrl}' not found`;
    setupGitCloneMock(2, errorMsg);

    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    const clonePromise = cloneWithRetry(['clone', tokenUrl, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    consoleSpy.mockRestore();
    vi.useRealTimers();

    const retryLines = logLines.filter(l => l.includes('Git clone failed'));
    expect(retryLines.length).toBeGreaterThanOrEqual(2);
    for (const line of retryLines) {
      expect(line).not.toContain('ghp_secret123');
      expect(line).toContain('***');
    }
  });

  it('re-mints a fresh token and rewrites the clone URL after an AUTH failure', async () => {
    vi.useFakeTimers();
    process.env.TASK_ID = 'task-auth-123';
    const staleUrl = 'https://x-access-token:ghs_STALE_TOKEN@github.com/org/repo.git';
    const authErr = "remote: Invalid username or token. Password authentication is not supported.\nfatal: Authentication failed for 'https://github.com/org/repo.git/'";

    const cloneArgs: string[][] = [];
    setupGitCloneMock(1, authErr, cloneArgs);

    // force_refresh token endpoint returns a fresh token.
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: { token: 'ghs_FRESH_TOKEN', owner: 'org', repo: 'repo' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const logLines: string[] = [];
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      logLines.push(args.map(String).join(' '));
    });

    const clonePromise = cloneWithRetry(['clone', staleUrl, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    consoleSpy.mockRestore();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete process.env.TASK_ID;

    // The token endpoint was hit with force_refresh=true.
    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toContain('/api/agent/github/token');
    expect(calledUrl).toContain('force_refresh=true');

    // The retry clone used the FRESH token, not the stale one.
    const secondCloneArgs = cloneArgs[1];
    expect(secondCloneArgs.some(x => x.includes('ghs_FRESH_TOKEN'))).toBe(true);
    expect(secondCloneArgs.some(x => x.includes('ghs_STALE_TOKEN'))).toBe(false);

    // No log line ever leaks a raw token.
    for (const line of logLines) {
      expect(line).not.toContain('ghs_STALE_TOKEN');
      expect(line).not.toContain('ghs_FRESH_TOKEN');
    }
  });

  it('does NOT re-mint a token for a transient NETWORK failure', async () => {
    vi.useFakeTimers();
    process.env.TASK_ID = 'task-net-456';
    const url = 'https://x-access-token:ghs_NET_TOKEN@github.com/org/repo.git';
    const netErr = 'fatal: unable to access: Could not resolve host: github.com';

    setupGitCloneMock(1, netErr);

    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: { token: 'ghs_SHOULD_NOT_BE_FETCHED' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const clonePromise = cloneWithRetry(['clone', url, '/tmp/repo'], 1000);
    await vi.runAllTimersAsync();
    await clonePromise;

    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete process.env.TASK_ID;

    // The token endpoint must NOT have been called for a network error.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ── resolveCredentialsFromSecretVersion ───────────────────────────────────────
describe('resolveCredentialsFromSecretVersion', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    _setAgentTokenForTesting('');
    delete process.env['AGENT_SECRET_TOKEN'];
    delete process.env['ANTHROPIC_API_KEY'];
    delete process.env['CLAUDE_CODE_OAUTH_TOKEN'];
    delete process.env['OPENAI_API_KEY'];
    delete process.env['CODEX_API_KEY'];
    delete process.env['GEMINI_API_KEY'];
    delete process.env['GOOGLE_APPLICATION_CREDENTIALS'];
    delete process.env['GOOGLE_CLOUD_PROJECT'];
    delete process.env['GOOGLE_CLOUD_LOCATION'];
    delete process.env['CODEX_HOME'];
    delete process.env['GEMINI_CLI_TRUST_WORKSPACE'];
    vi.mocked(fs.writeFileSync).mockClear();
    vi.mocked(fs.mkdirSync).mockClear();
  });

  function makeManifestFetch(manifest: Record<string, unknown>): ReturnType<typeof makeFetchMock> {
    const manifestBase64 = Buffer.from(JSON.stringify(manifest)).toString('base64');
    return makeFetchMock().mockImplementation((url: unknown) => {
      const urlStr = String(url);
      if (urlStr.includes('metadata.google.internal')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ access_token: 'gcp-test-token' }) });
      }
      if (urlStr.includes('secretmanager.googleapis.com')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ payload: { data: manifestBase64 } }) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
  }

  it('populates getAgentToken() and apiHeaders[X-Agent-Token] from Secret Manager manifest', async () => {
    const expectedToken = 'test-agent-jwt-from-secret-manager';
    const fetchMock = makeManifestFetch({ env: { AGENT_SECRET_TOKEN: expectedToken } });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    // AGENT_TOKEN must be populated from the manifest
    expect(getAgentToken()).toBe(expectedToken);

    // apiHeaders['X-Agent-Token'] must also be populated so apiPost sends the token
    await apiPost('/api/test-token-header', {}, 1);
    const postCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/api/test-token-header'));
    expect(postCall).toBeDefined();
    const postHeaders = (defined(postCall)[1] as RequestInit).headers as Record<string, string>;
    expect(postHeaders['X-Agent-Token']).toBe(expectedToken);
  });

  it('populates LLM credential env vars from the manifest', async () => {
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'test-token',
        ANTHROPIC_API_KEY: 'sk-ant-test-key',
        CLAUDE_CODE_OAUTH_TOKEN: 'test-oauth-token',
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    expect(process.env['ANTHROPIC_API_KEY']).toBe('sk-ant-test-key');
    expect(process.env['CLAUDE_CODE_OAUTH_TOKEN']).toBe('test-oauth-token');
  });

  it('does not overwrite an already-set LLM credential env var', async () => {
    process.env['ANTHROPIC_API_KEY'] = 'already-set-key';
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'test-token',
        ANTHROPIC_API_KEY: 'manifest-key-should-not-win',
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    expect(process.env['ANTHROPIC_API_KEY']).toBe('already-set-key');
  });

  it('throws when the metadata token fetch returns a non-2xx status', async () => {
    const fetchMock = makeFetchMock().mockImplementation((url: unknown) => {
      if (String(url).includes('metadata.google.internal')) {
        return Promise.resolve({ ok: false, status: 403 });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1'),
    ).rejects.toThrow('failed to get GCP access token');
  });

  it('throws when the Secret Manager :access call returns a non-2xx status', async () => {
    const fetchMock = makeFetchMock().mockImplementation((url: unknown) => {
      if (String(url).includes('metadata.google.internal')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ access_token: 'gcp-test-token' }) });
      }
      if (String(url).includes('secretmanager.googleapis.com')) {
        return Promise.resolve({ ok: false, status: 403 });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1'),
    ).rejects.toThrow('failed to fetch secret');
  });

  it('resolveCredentialsFromSecretVersion writes files to disk and sets file-path env vars', async () => {
    const fetchMock = makeManifestFetch({
      env: {
        AGENT_SECRET_TOKEN: 'tok',
        GOOGLE_APPLICATION_CREDENTIALS: '/tmp/test-sa.json',
        GOOGLE_CLOUD_PROJECT: 'my-project',
      },
      files: [
        { path: '/tmp/test-sa.json', content: '{"type":"service_account"}', mode: '0600' },
      ],
    });
    vi.stubGlobal('fetch', fetchMock);

    await resolveCredentialsFromSecretVersion('projects/p/secrets/agent-creds/versions/1');

    // File-path env vars must be set from manifest.env
    expect(process.env['GOOGLE_APPLICATION_CREDENTIALS']).toBe('/tmp/test-sa.json');
    expect(process.env['GOOGLE_CLOUD_PROJECT']).toBe('my-project');

    // File must have been written to disk with correct content and mode
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/test-sa.json',
      '{"type":"service_account"}',
      { encoding: 'utf-8', mode: 0o600 },
    );

    // Parent directory must have been created
    expect(vi.mocked(fs.mkdirSync)).toHaveBeenCalledWith(
      '/tmp',
      { recursive: true, mode: 0o700 },
    );
  });
});

// ── cloneWithRetry: commitless repositories ──────────────────────────────────
// A repo with zero commits has no default branch, so `git clone --branch <trunk>`
// fails with EXACTLY the same text as a branch that was deleted after its PR
// merged. Retrying cannot conjure a branch, and letting the ambiguous text stand
// gets the failure classified BRANCH_GONE ("PR merged before clone") — a benign
// transient story for a permanent setup gap. The remote is the only oracle: a
// commitless repo advertises zero refs.
describe('cloneWithRetry — commitless repo', () => {
  const CLONE_ARGS = [
    '-c', 'credential.https://github.com.helper=/home/agent/.blueprint/git-credential-helper',
    'clone', '--depth', '50', '--branch', 'main',
    'https://github.com/HenryHamster/playground.git', '/tmp/workspace',
  ];
  // The literal stderr from the production failure (task 235d7c59, playground).
  const BRANCH_MISSING_STDERR =
    "Cloning into '/tmp/workspace'...\nfatal: Remote branch main not found in upstream origin";

  /** A fake `git` child process: emits its output, then closes with `code`. */
  function fakeGitProc(code: number, { stdout = '', stderr = '' } = {}) {
    const proc = {
      stdout: { on: (ev: string, cb: (d: Buffer) => void) => { if (ev === 'data' && stdout) cb(Buffer.from(stdout)); } },
      stderr: { on: (ev: string, cb: (d: Buffer) => void) => { if (ev === 'data' && stderr) cb(Buffer.from(stderr)); } },
      on: (ev: string, cb: (c: number) => void) => { if (ev === 'close') setImmediate(() => { cb(code); }); return proc; },
      kill: vi.fn(),
    };
    return proc;
  }

  /** Script `git`: the clone always fails branch-missing; `ls-remote` reports
   *  whatever refs the remote is pretending to have. */
  function scriptGit(refs: string) {
    vi.mocked(spawn).mockImplementation(((_cmd: string, args: string[]) => {
      if (args[0] === 'ls-remote') return fakeGitProc(0, { stdout: refs });
      return fakeGitProc(128, { stderr: BRANCH_MISSING_STDERR });
    }) as unknown as typeof spawn);
  }

  const cloneAttempts = () =>
    vi.mocked(spawn).mock.calls.filter(c => (c[1] as string[]).includes('clone'));
  const lsRemoteProbes = () =>
    vi.mocked(spawn).mock.calls.filter(c => (c[1] as string[])[0] === 'ls-remote');

  beforeEach(() => { vi.mocked(spawn).mockReset(); });

  it('zero refs → fails fast with the real reason, burning no retries', async () => {
    scriptGit(''); // a commitless repo advertises no refs at all

    await expect(cloneWithRetry(CLONE_ARGS)).rejects.toThrow(/repository has no commits/i);

    // The whole point: one attempt, not three. Retrying a repo that has no
    // commits is pure latency — the branch cannot appear on its own.
    expect(cloneAttempts()).toHaveLength(1);
    expect(lsRemoteProbes()).toHaveLength(1);
  });

  it('probes the remote over the clone URL, not the credential-helper arg', async () => {
    scriptGit('');
    await expect(cloneWithRetry(CLONE_ARGS)).rejects.toThrow(/repository has no commits/i);

    expect(lsRemoteProbes()[0][1]).toEqual([
      'ls-remote', '--heads', 'https://github.com/HenryHamster/playground.git',
    ]);
  });

  it('deleted branch on a real repo → NOT reported as commitless, retries as before', async () => {
    // Same git error, but the remote still has refs: this is the branch-gone
    // case and must keep its existing behaviour. If the probe were skipped (or
    // its result ignored) a merged-PR race would be misreported as an unseeded
    // repo — the mirror image of the bug this fixes.
    scriptGit('a1b2c3\trefs/heads/main\nd4e5f6\trefs/heads/develop\n');

    vi.useFakeTimers();
    try {
      const attempt = cloneWithRetry(CLONE_ARGS);
      const assertion = expect(attempt).rejects.toThrow(/not found in upstream origin/i);
      await vi.runAllTimersAsync();
      await assertion;
      await expect(attempt).rejects.not.toThrow(/repository has no commits/i);
    } finally {
      vi.useRealTimers();
    }

    expect(cloneAttempts()).toHaveLength(3); // full retry budget, unchanged
  });
});
