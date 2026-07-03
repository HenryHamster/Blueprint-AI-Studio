import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Hoisted setup ─────────────────────────────────────────────────────────────
const { mockRecordToolUse, mockRecordCheckOutcome, mockIsCheckInvocation, mockSendLog } =
  vi.hoisted(() => {
    process.env.PROVIDER = 'anthropic';
    process.env.AUTH_MODE = 'oauth';
    delete process.env.LLM_CLI_TRANSLATION;
    delete process.env.DISABLE_CHECK_RUNAWAY_WATCHDOG;
    return {
      mockRecordToolUse: vi.fn(),
      mockRecordCheckOutcome: vi.fn(),
      mockIsCheckInvocation: vi.fn((s: string) => /\.\/check(?:\s|")/.test(s)),
      mockSendLog: vi.fn(),
    };
  });

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('fs', () => ({
  default: {
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    openSync: vi.fn().mockReturnValue(3),
    closeSync: vi.fn(),
    chmodSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
  },
}));

vi.mock('child_process', () => ({
  spawn: vi.fn(),
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '',
  getAgentToken: vi.fn().mockReturnValue(null),
  log: vi.fn(),
  sendLog: mockSendLog,
  strVal: vi.fn((v: unknown) => (typeof v === 'string' ? v : '')),
  recordToolUse: mockRecordToolUse,
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  estimateTokensFromChars: vi.fn().mockReturnValue(0),
  sanitize: vi.fn((s: string) => s),
  ClaudeTimeoutError: class ClaudeTimeoutError extends Error {
    constructor(msg: string) { super(msg); this.name = 'ClaudeTimeoutError'; }
  },
}));

vi.mock('../fix-loop-events.js', () => ({
  isCheckInvocation: mockIsCheckInvocation,
  recordCheckOutcome: mockRecordCheckOutcome,
  getCheckRunawayStatus: vi.fn().mockReturnValue({ totalChecks: 0, shouldTrigger: false }),
}));

vi.mock('../processTracking.js', () => ({
  registerChildPid: vi.fn(),
  unregisterChildPid: vi.fn(),
}));

// runners.ts now imports the playbook/messages writers from ../prompt.js for
// ambient helper provisioning (provisionAmbientHelpers). Stub them so importing
// runners.ts does not transitively load the real prompt subsystem under this
// file's partial ../index.js mock. The ambient block is a guarded no-op here
// anyway (fs is fully mocked), so these are never meaningfully exercised.
vi.mock('../prompt.js', () => ({
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
}));

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import { spawn } from 'child_process';
import {
  runProviderAgent,
  getLastProviderRunErrorReport,
  _setLastProviderRunErrorReportForTesting,
  startResidentClaudeSession,
} from './runners.js';
import { CLAUDE_RESULT_GRACE_MS } from '../constants.js';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import { recordLlmTokens } from '../index.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

type SpawnMock = ReturnType<typeof spawn>;

/**
 * Creates a mock spawn that emits each JSON line as a separate stdout data
 * event, then fires 'close' one setImmediate tick later.  All events are
 * queued via the real setImmediate so the fake-less tests keep real timer
 * semantics.
 */
function emitStream(jsonLines: string[], exitCode = 0): void {
  vi.mocked(spawn).mockImplementationOnce(
    (_cmd: string, _args: readonly string[], _opts?: unknown) => {
      const stdoutCbs: Array<(d: Buffer) => void> = [];
      const closeCbs: Array<(code: number | null, signal: NodeJS.Signals | null) => void> = [];
      const proc = {
        pid: 12345,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdin: { on: vi.fn(), end: vi.fn() },
        stdout: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stdoutCbs.push(cb);
          }),
        },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
          if (event === 'close') {
            closeCbs.push(cb as (code: number | null, signal: NodeJS.Signals | null) => void);
          }
        }),
        kill: vi.fn(),
      };
      setImmediate(() => {
        for (const line of jsonLines) {
          for (const cb of stdoutCbs) cb(Buffer.from(line + '\n'));
        }
        setImmediate(() => {
          proc.exitCode = exitCode;
          for (const cb of closeCbs) cb(exitCode, null);
        });
      });
      return proc as unknown as SpawnMock;
    },
  );
}

// ── Test suites ───────────────────────────────────────────────────────────────

describe('drainJsonChunks — assistant event dispatch', () => {
  beforeEach(() => {
    mockRecordToolUse.mockClear();
    mockRecordCheckOutcome.mockClear();
    mockIsCheckInvocation.mockClear();
    mockSendLog.mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('calls recordToolUse for a tool_use block', async () => {
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'echo hi' } }] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(mockRecordToolUse).toHaveBeenCalledOnce();
    expect(mockRecordToolUse).toHaveBeenCalledWith('Bash', { command: 'echo hi' });
  });

  it('does NOT call recordToolUse when event type is "message" — regression trap for dropped dispatch key', async () => {
    // If runners.ts reverts from 'assistant' to 'message', this test passes
    // but the test above fails.  Together they pin the exact dispatch key.
    emitStream([
      JSON.stringify({
        type: 'message',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: {} }] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(mockRecordToolUse).not.toHaveBeenCalled();
  });

  it('updates statusRef.value to the first 100 chars of a text block', async () => {
    const longText = 'a'.repeat(200);
    const statusRef = { value: '' };
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'text', text: longText }] },
      }),
    ]);
    await runProviderAgent('prompt', null, statusRef);
    expect(statusRef.value).toBe('a'.repeat(100));
  });

  it('updates statusRef.value for a tool_use block', async () => {
    const statusRef = { value: '' };
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Read', input: {} }] },
      }),
    ]);
    await runProviderAgent('prompt', null, statusRef);
    expect(statusRef.value).toBe('Using tool: Read');
  });
});

describe('drainJsonChunks — result event dispatch', () => {
  beforeEach(() => {
    mockSendLog.mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('populates lastResultText from obj.result', async () => {
    emitStream([
      JSON.stringify({ type: 'result', result: 'Task completed successfully.' }),
    ]);
    const { lastResultText } = await runProviderAgent('prompt', null, { value: '' });
    expect(lastResultText).toBe('Task completed successfully.');
  });

  it('arms the grace timer (setTimeout with CLAUDE_RESULT_GRACE_MS) after a result event', async () => {
    const setTimeoutSpy = vi.spyOn(global, 'setTimeout');
    try {
      emitStream([
        JSON.stringify({ type: 'result', result: 'done' }),
      ]);
      await runProviderAgent('prompt', null, { value: '' });
      const graceCalls = setTimeoutSpy.mock.calls.filter(call => call[1] === CLAUDE_RESULT_GRACE_MS);
      expect(graceCalls).toHaveLength(1);
    } finally {
      setTimeoutSpy.mockRestore();
    }
  });

  it('clears the grace timer when the process closes after a result event', async () => {
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');
    try {
      emitStream([
        JSON.stringify({ type: 'result', result: 'done' }),
      ]);
      await runProviderAgent('prompt', null, { value: '' });
      // clearTimeout must be called in the close handler to prevent stray kills
      expect(clearTimeoutSpy).toHaveBeenCalled();
    } finally {
      clearTimeoutSpy.mockRestore();
    }
  });
});

describe('drainJsonChunks — check-runaway detection (assistant + user events)', () => {
  beforeEach(() => {
    mockRecordCheckOutcome.mockClear();
    mockIsCheckInvocation.mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('calls isCheckInvocation and records an outcome when Bash tool invokes ./check', async () => {
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: './check test' } }] },
      }),
      JSON.stringify({ type: 'user' }),
    ]);
    await runProviderAgent('prompt', null, { value: '' }, undefined, undefined, undefined, true);
    expect(mockIsCheckInvocation).toHaveBeenCalledOnce();
    expect(mockRecordCheckOutcome).toHaveBeenCalledOnce();
  });

  it('does NOT record an outcome for non-check Bash invocations', async () => {
    mockIsCheckInvocation.mockReturnValueOnce(false);
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'npm test' } }] },
      }),
      JSON.stringify({ type: 'user' }),
    ]);
    await runProviderAgent('prompt', null, { value: '' }, undefined, undefined, undefined, true);
    expect(mockRecordCheckOutcome).not.toHaveBeenCalled();
  });

  it('does NOT call isCheckInvocation for non-Bash tools', async () => {
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Read', input: { file_path: './check' } }] },
      }),
      JSON.stringify({ type: 'user' }),
    ]);
    await runProviderAgent('prompt', null, { value: '' }, undefined, undefined, undefined, true);
    expect(mockIsCheckInvocation).not.toHaveBeenCalled();
    expect(mockRecordCheckOutcome).not.toHaveBeenCalled();
  });

  it('passes a string of at most 200 chars to isCheckInvocation (slice cap)', async () => {
    // JSON.stringify({ command: longCommand }) produces >200 chars;
    // the runner must slice before calling isCheckInvocation.
    const longCommand = './check test ' + 'x'.repeat(300);
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: longCommand } }] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' }, undefined, undefined, undefined, true);
    expect(mockIsCheckInvocation).toHaveBeenCalledOnce();
    const [arg] = mockIsCheckInvocation.mock.calls[0] as [string];
    expect(arg.length).toBeLessThanOrEqual(200);
    // Confirm the raw JSON would have exceeded 200 chars — otherwise the test proves nothing
    expect(JSON.stringify({ command: longCommand }).length).toBeGreaterThan(200);
  });
});

describe('structured provider-run error report', () => {
  let savedTaskId: string | undefined;

  beforeEach(() => {
    vi.mocked(spawn).mockReset();
    mockSendLog.mockClear();
    _setLastProviderRunErrorReportForTesting(null);
    savedTaskId = process.env.TASK_ID;
    process.env.TASK_ID = 'task-report-1';
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
    else delete process.env.TASK_ID;
    _setLastProviderRunErrorReportForTesting(null);
  });

  it('records a startup-death report: non-zero exit with init banner only and no model output', async () => {
    // The 2026-06-06 incident signature: the CLI prints its init system event
    // then dies on the first API call (~5s, exit 1). The report must prove the
    // model never produced output so the backend routes this to the
    // rate-limit/transient path, never CODE_ERROR.
    emitStream([JSON.stringify({ type: 'system', subtype: 'init' })], 1);

    await runProviderAgent('prompt', null, { value: '' });

    const report = getLastProviderRunErrorReport();
    expect(report).toMatchObject({
      schema: 1,
      task_id: 'task-report-1',
      provider: 'anthropic',
      auth_mode: 'oauth',
      exit_code: 1,
      model_output_seen: false,
      result_event_seen: false,
      last_stream_event: 'system',
    });
    expect(report?.provider_errors).toEqual([]);
  });

  it('records a completed-run report: model output + terminal result event on clean exit', async () => {
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'text', text: 'working on it' }] },
      }),
      JSON.stringify({ type: 'result', result: 'done' }),
    ], 0);

    await runProviderAgent('prompt', null, { value: '' });

    const report = getLastProviderRunErrorReport();
    expect(report).toMatchObject({
      exit_code: 0,
      model_output_seen: true,
      result_event_seen: true,
      last_stream_event: 'result',
    });
  });

  it('a genuine failed-but-completed run reports result_event_seen=true (stays code-error territory)', async () => {
    // Model ran, produced output, and reached its terminal result — the
    // non-zero exit belongs to the work, not the provider. The backend must
    // NOT treat this report as a provider death.
    emitStream([
      JSON.stringify({
        type: 'assistant',
        message: { content: [{ type: 'text', text: 'attempting fix' }] },
      }),
      JSON.stringify({ type: 'result', result: 'could not complete' }),
    ], 1);

    await runProviderAgent('prompt', null, { value: '' });

    const report = getLastProviderRunErrorReport();
    expect(report).toMatchObject({
      exit_code: 1,
      model_output_seen: true,
      result_event_seen: true,
    });
  });

  it('captures structured stream error events into provider_errors with code/type', async () => {
    emitStream([
      JSON.stringify({ type: 'system', subtype: 'init' }),
      JSON.stringify({ type: 'error', error: { message: 'Overloaded', type: 'overloaded_error' } }),
    ], 1);

    await runProviderAgent('prompt', null, { value: '' });

    const report = getLastProviderRunErrorReport();
    expect(report?.provider_errors).toEqual([
      { source: 'claude-stream', message: 'Overloaded', code: 'overloaded_error' },
    ]);
    expect(report?.model_output_seen).toBe(false);
  });

  it('bounds provider_errors to 5 entries', async () => {
    const errorLines = Array.from({ length: 8 }, (_, i) =>
      JSON.stringify({ type: 'error', error: { message: `err-${i}` } }),
    );
    emitStream(errorLines, 1);

    await runProviderAgent('prompt', null, { value: '' });

    expect(getLastProviderRunErrorReport()?.provider_errors).toHaveLength(5);
  });
});

describe('drainJsonChunks — dispatch completeness', () => {
  beforeEach(() => {
    mockSendLog.mockClear();
    vi.mocked(spawn).mockReset();
  });

  it.each([
    ['assistant', { type: 'assistant', message: { content: [] } }],
    ['result', { type: 'result', result: '' }],
    ['error', { type: 'error', error: { message: 'oops' } }],
    ['system', { type: 'system' }],
    ['user', { type: 'user' }],
  ])('event type "%s" does not fall through to the unknown-type branch', async (eventType, payload) => {
    emitStream([JSON.stringify(payload)]);
    await runProviderAgent('prompt', null, { value: '' });
    const unknownCalls = mockSendLog.mock.calls.filter(
      (args: unknown[]) =>
        typeof args[1] === 'string' && args[1].includes(`[claude-stream unknown type: ${eventType}]`),
    );
    expect(unknownCalls).toHaveLength(0);
  });
});

describe('spawn stdio validation — no-stdio branch does not leak PID or timers', () => {
  beforeEach(() => {
    vi.mocked(registerChildPid).mockClear();
    vi.mocked(unregisterChildPid).mockClear();
    mockSendLog.mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('resolves with exitCode=1 and never registers the PID when stdout is null', async () => {
    vi.mocked(spawn).mockImplementationOnce(
      (_cmd: string, _args: readonly string[], _opts?: unknown) =>
        ({
          pid: 77777,
          exitCode: null,
          signalCode: null,
          stdin: { on: vi.fn(), end: vi.fn() },
          stdout: null,
          stderr: { on: vi.fn() },
          on: vi.fn(),
          kill: vi.fn(),
        }) as unknown as SpawnMock,
    );
    const result = await runProviderAgent('prompt', null, { value: '' });
    expect(result.exitCode).toBe(1);
    expect(vi.mocked(registerChildPid)).not.toHaveBeenCalled();
    expect(vi.mocked(unregisterChildPid)).not.toHaveBeenCalled();
  });

  it('resolves with exitCode=1 and never registers the PID when stderr is null', async () => {
    vi.mocked(spawn).mockImplementationOnce(
      (_cmd: string, _args: readonly string[], _opts?: unknown) =>
        ({
          pid: 77778,
          exitCode: null,
          signalCode: null,
          stdin: { on: vi.fn(), end: vi.fn() },
          stdout: { on: vi.fn() },
          stderr: null,
          on: vi.fn(),
          kill: vi.fn(),
        }) as unknown as SpawnMock,
    );
    const result = await runProviderAgent('prompt', null, { value: '' });
    expect(result.exitCode).toBe(1);
    expect(vi.mocked(registerChildPid)).not.toHaveBeenCalled();
    expect(vi.mocked(unregisterChildPid)).not.toHaveBeenCalled();
  });

  it('logs an abort message when stdio streams are missing', async () => {
    vi.mocked(spawn).mockImplementationOnce(
      (_cmd: string, _args: readonly string[], _opts?: unknown) =>
        ({
          pid: 77779,
          exitCode: null,
          signalCode: null,
          stdin: { on: vi.fn(), end: vi.fn() },
          stdout: null,
          stderr: null,
          on: vi.fn(),
          kill: vi.fn(),
        }) as unknown as SpawnMock,
    );
    await runProviderAgent('prompt', null, { value: '' });
    const abortCalls = mockSendLog.mock.calls.filter(
      (args: unknown[]) =>
        typeof args[1] === 'string' && args[1].includes('no stdio streams'),
    );
    expect(abortCalls.length).toBeGreaterThan(0);
  });
});

// ── Resident session state machine ────────────────────────────────────────────

/**
 * A controllable fake `claude` child for the resident session: captures the
 * stdout 'data' + 'close' handlers so a test can push stream-json lines on
 * demand and drive lifecycle events, and records stdin writes so turn/interrupt
 * encoding can be asserted.
 */
function makeControllableChild() {
  let dataCb: ((d: Buffer) => void) | null = null;
  let closeCb: ((code: number | null, signal: NodeJS.Signals | null) => void) | null = null;
  const stdinWrites: string[] = [];
  const proc = {
    pid: 4242,
    exitCode: null as number | null,
    signalCode: null as NodeJS.Signals | null,
    stdin: {
      destroyed: false,
      write: vi.fn((s: string) => { stdinWrites.push(s); return true; }),
      end: vi.fn(),
    },
    stdout: {
      on: vi.fn((event: string, cb: (d: Buffer) => void) => { if (event === 'data') dataCb = cb; }),
    },
    stderr: { on: vi.fn() },
    on: vi.fn((event: string, cb: (...a: unknown[]) => void) => {
      if (event === 'close') closeCb = cb;
    }),
    kill: vi.fn(),
  };
  return {
    proc: proc as unknown as SpawnMock,
    stdinWrites,
    pushLine: (obj: unknown) => dataCb?.(Buffer.from(JSON.stringify(obj) + '\n')),
    triggerClose: (code = 0) => { proc.exitCode = code; closeCb?.(code, null); },
  };
}

const STRATEGIC_CREATE = './strategic create-direction \'{}\'';

describe('startResidentClaudeSession', () => {
  beforeEach(() => {
    mockSendLog.mockClear();
    vi.mocked(spawn).mockReset();
    vi.mocked(registerChildPid).mockClear();
    vi.mocked(unregisterChildPid).mockClear();
    vi.spyOn(process, 'kill').mockReturnValue(true); // never signal a real pid in tests
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('appends --input-format stream-json and keeps stdin piped', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const [, args, opts] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[], Record<string, unknown>];
    expect(args).toContain('--input-format');
    expect(args).toContain('stream-json');
    expect(args).toContain('-p');
    expect(args).toContain('--include-partial-messages');
    expect(opts.stdio).toEqual(['pipe', 'pipe', 'pipe']);
    expect(opts.detached).toBe(true);
    expect(registerChildPid).toHaveBeenCalledWith(4242);
    session.close();
  });

  it('sendTurn writes a user NDJSON line and resolves on the top-level result', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const events: Array<{ kind: string; text?: string }> = [];
    session.onEvent((e) => events.push(e));

    const turn = session.sendTurn('hello there');
    const userLine = JSON.parse(ctl.stdinWrites[0].trim()) as Record<string, unknown>;
    expect(userLine).toEqual({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: 'hello there' }] } });

    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'hi ' } } });
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'back' } } });
    ctl.pushLine({ type: 'result', result: 'hi back', session_id: 'sess-1' });

    const r = await turn;
    expect(r.resultText).toBe('hi back');
    expect(r.interrupted).toBe(false);
    expect(events.filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(['hi ', 'back']);
    expect(session.sessionId()).toBe('sess-1');
    session.close();
  });

  it('does NOT re-emit finalized assistant text after streaming its deltas', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const texts: string[] = [];
    session.onEvent((e) => { if (e.kind === 'text' && e.text) texts.push(e.text); });

    const turn = session.sendTurn('q');
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'answer' } } });
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'text', text: 'answer' }] } });
    ctl.pushLine({ type: 'result', result: 'answer' });
    await turn;
    expect(texts).toEqual(['answer']);
    session.close();
  });

  it('flags an applied strategic op when a mutating ./strategic Bash command runs', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('advance');
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: STRATEGIC_CREATE } }] } });
    ctl.pushLine({ type: 'result', result: 'done' });
    const r = await turn;
    expect(r.sawToolUse).toBe(true);
    expect(r.appliedStrategicOp).toBe(true);
    session.close();
  });

  it('does not flag an applied op for a read-only ./fetch', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('advance');
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: './fetch problem-model' } }] } });
    ctl.pushLine({ type: 'result', result: 'looked' });
    const r = await turn;
    expect(r.sawToolUse).toBe(true);
    expect(r.appliedStrategicOp).toBe(false);
    session.close();
  });

  it('interrupt writes a control_request and marks the turn interrupted', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('long task');
    session.interrupt();
    const controlWrite = ctl.stdinWrites.find((w) => w.includes('control_request'));
    expect(controlWrite).toBeTruthy();
    const parsed = JSON.parse((controlWrite as string).trim()) as { request?: unknown };
    expect(parsed.request).toEqual({ subtype: 'interrupt' });
    ctl.pushLine({ type: 'result', result: 'stopped' });
    const r = await turn;
    expect(r.interrupted).toBe(true);
    session.close();
  });

  it('settles a pending turn as processGone when the child closes', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('will die');
    ctl.triggerClose(1);
    const r = await turn;
    expect(r.processGone).toBe(true);
    expect(session.isAlive()).toBe(false);
    expect(session.exitCode()).toBe(1);
    expect(unregisterChildPid).toHaveBeenCalledWith(4242);
  });

  it('close ends stdin and stops reporting alive', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    expect(session.isAlive()).toBe(true);
    session.close();
    expect((ctl.proc as unknown as { stdin: { end: ReturnType<typeof vi.fn> } }).stdin.end).toHaveBeenCalled();
    expect(session.isAlive()).toBe(false);
  });

  it('records per-turn token usage from the result event', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(recordLlmTokens).mockClear();
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('q');
    ctl.pushLine({
      type: 'result', result: 'a',
      usage: { input_tokens: 50, output_tokens: 20, cache_read_input_tokens: 5 },
    });
    await turn;
    // inputTokens folds in the cache classes: 50 + 0 + 5 = 55.
    expect(recordLlmTokens).toHaveBeenCalledWith(55, 20, 0, 5);
    session.close();
  });

  it('flags sentOperatorMessage only after ./messages send SUCCEEDS (tool_result, no error)', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_send', name: 'Bash', input: { command: "./messages send 'here you go'" } }] },
    });
    // The send's tool_result comes back without an error → confirmed delivery.
    ctl.pushLine({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_send', content: 'ok', is_error: false }] },
    });
    ctl.pushLine({ type: 'result', result: 'done' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(true);
    session.close();
  });

  it('does NOT flag sentOperatorMessage when ./messages send FAILS (tool_result is_error)', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_bad', name: 'Bash', input: { command: "./messages send 'lost reply'" } }] },
    });
    // The curl failed → is_error: the flag must stay false so persistReply runs.
    ctl.pushLine({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_bad', content: 'curl: (7) failed', is_error: true }] },
    });
    ctl.pushLine({ type: 'result', result: 'tried' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(false);
    session.close();
  });

  it('does NOT flag sentOperatorMessage when the send has no confirming tool_result', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_x', name: 'Bash', input: { command: "./messages send 'unconfirmed'" } }] },
    });
    // Turn ends before any tool_result — unconfirmed, so persistReply backstops.
    ctl.pushLine({ type: 'result', result: 'done' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(false);
    session.close();
  });

  it('does NOT flag sentOperatorMessage for a turn that never posts a reply', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('advance');
    ctl.pushLine({ type: 'result', result: 'noted' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(false);
    session.close();
  });

  it('touch() resets the idle ceiling so a chat-only session does not self-close', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ idleCeilingMs: 10_000 });
    // External activity (a heartbeat/poll) just before the ceiling re-arms it.
    await vi.advanceTimersByTimeAsync(9_000);
    session.touch();
    await vi.advanceTimersByTimeAsync(9_000);
    expect(session.isAlive()).toBe(true); // would have closed at 10s without touch
    // With no further touches the ceiling still fires — the backstop is intact.
    await vi.advanceTimersByTimeAsync(11_000);
    expect(session.isAlive()).toBe(false);
  });

  it('hard-timeout force-kills a wedged turn and settles it FAILED (hardTimedOut + processGone)', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const killSpy = vi.spyOn(process, 'kill');
    killSpy.mockClear();
    // Hard ceiling (5s) below the stall budget (60s): the output-driven stall
    // watchdog never gets a chance — the ABSOLUTE hard timer is what fires.
    const session = startResidentClaudeSession({ turnTimeoutMs: 60_000, hardTurnTimeoutMs: 5_000 });
    const turn = session.sendTurn('wedge — never returns a result');
    // No stream output at all. Advance past hard-timeout + interrupt grace + kill.
    await vi.advanceTimersByTimeAsync(12_000);
    const r = await turn;
    expect(r.hardTimedOut).toBe(true);
    expect(r.timedOut).toBe(true);
    expect(r.processGone).toBe(true);
    expect(session.isAlive()).toBe(false);
    // Escalation actually signalled the process GROUP (negative pid).
    expect(killSpy.mock.calls.some(([p]) => p === -4242)).toBe(true);
  });

  it('does not let a force-settled turn’s late result resolve a NEWER turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    // Large hard ceiling so only the stall→interrupt→force-settle path runs here.
    const session = startResidentClaudeSession({ turnTimeoutMs: 20_000, hardTurnTimeoutMs: 10 * 60_000 });

    const turnA = session.sendTurn('A');
    // No output → stall (20s) → interrupt → force-settle (+20s), process alive.
    await vi.advanceTimersByTimeAsync(41_000);
    const rA = await turnA;
    expect(rA.timedOut).toBe(true);
    expect(rA.processGone).toBe(false);
    expect(session.isAlive()).toBe(true);

    // A new turn B on the still-alive session.
    const turnB = session.sendTurn('B');
    let bResolved = false;
    void turnB.then(() => { bResolved = true; });

    // A's LATE result finally arrives — it must be SWALLOWED, not resolve B.
    ctl.pushLine({ type: 'result', result: 'A-late' });
    await Promise.resolve();
    await Promise.resolve();
    expect(bResolved).toBe(false);

    // B's own result then resolves B with B's text.
    ctl.pushLine({ type: 'result', result: 'B-done' });
    const rB = await turnB;
    expect(rB.resultText).toBe('B-done');
    session.close();
  });

  it('drops a force-settled turn’s late INTERMEDIATE events instead of attributing them to a NEWER turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ turnTimeoutMs: 20_000, hardTurnTimeoutMs: 10 * 60_000 });
    const texts: string[] = [];
    session.onEvent((e) => { if (e.kind === 'text' && e.text) texts.push(e.text); });

    const turnA = session.sendTurn('A');
    // No output → stall (20s) → interrupt → force-settle (+20s); process alive so
    // A still owes one late `result` (staleResultsPending === 1).
    await vi.advanceTimersByTimeAsync(41_000);
    const rA = await turnA;
    expect(rA.timedOut).toBe(true);
    expect(rA.processGone).toBe(false);
    expect(session.isAlive()).toBe(true);

    // A NEW turn B on the still-alive session.
    const turnB = session.sendTurn('B');

    // A's LATE tail now streams (the child processes turns in order): a strategic
    // MUTATION tool_use + a text delta, THEN A's owed result. None of the
    // intermediates may touch B — they belong to the abandoned turn A.
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: STRATEGIC_CREATE } }] } });
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'A-tail' } } });
    ctl.pushLine({ type: 'result', result: 'A-late' }); // swallowed; staleResultsPending → 0

    // B's OWN output then flows and settles B with B's state only.
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'B-real' } } });
    ctl.pushLine({ type: 'result', result: 'B-done' });
    const rB = await turnB;

    expect(rB.resultText).toBe('B-done');
    // A's late strategic op / tool_use must NOT be mis-attributed to B.
    expect(rB.appliedStrategicOp).toBe(false);
    expect(rB.sawToolUse).toBe(false);
    // A's tail delta was dropped from the stream; only B's real delta reached it.
    expect(texts).toContain('B-real');
    expect(texts).not.toContain('A-tail');
    session.close();
  });

  it('does not mis-attribute a force-settled turn’s late ./messages send to the NEXT turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ turnTimeoutMs: 20_000, hardTurnTimeoutMs: 10 * 60_000 });

    const turnA = session.sendTurn('A');
    await vi.advanceTimersByTimeAsync(41_000);
    const rA = await turnA;
    expect(rA.timedOut).toBe(true);

    const turnB = session.sendTurn('B');
    // A's late confirmed ./messages send (tool_use + successful tool_result) must
    // NOT flip B.sentOperatorMessage — it was A's reply, not B's.
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_A', name: 'Bash', input: { command: "./messages send 'A reply'" } }] },
    });
    ctl.pushLine({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_A', content: 'ok', is_error: false }] },
    });
    ctl.pushLine({ type: 'result', result: 'A-late' }); // staleResultsPending → 0
    ctl.pushLine({ type: 'result', result: 'B-done' });
    const rB = await turnB;
    expect(rB.sentOperatorMessage).toBe(false);
    session.close();
  });
});
