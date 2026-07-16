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
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '',
  getAgentToken: vi.fn().mockReturnValue(null),
  log: vi.fn(),
  sendLog: mockSendLog,
  strVal: vi.fn((v: unknown) => (typeof v === 'string' ? v : '')),
  recordToolUse: mockRecordToolUse,
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  recordProviderReportedUsage: vi.fn(),
  recordNumTurns: vi.fn(),
  recordToolResultBytes: vi.fn(),
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
  writeSkillHelpers: vi.fn(),
  formatSkills: vi.fn(() => ''),
  formatProjectTools: vi.fn(() => ''),
  SHARED_LIFECYCLE_REMAINDER: '',
}));

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import { spawn } from 'child_process';
import {
  runProviderAgent,
  getLastProviderRunErrorReport,
  _setLastProviderRunErrorReportForTesting,
  __setCommandExistsOverrideForSmoke,
} from './runners.js';
import { recordLlmTokens, recordToolResultBytes } from '../index.js';
import { CLAUDE_RESULT_GRACE_MS } from '../constants.js';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

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
            closeCbs.push(cb);
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

// Bypass the pre-flight CLI-exists check for all tests in this file.
// Provider binaries (claude, codex, etc.) are not installed in the test
// environment; spawn is mocked separately per test.
beforeEach(() => {
  __setCommandExistsOverrideForSmoke(() => true);
});

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
    const [arg] = mockIsCheckInvocation.mock.calls[0];
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

describe('providerAutoInjectsClaudeMd', () => {
  it('returns true for anthropic', async () => {
    const { providerAutoInjectsClaudeMd } = await import('./runners.js');
    expect(providerAutoInjectsClaudeMd('anthropic')).toBe(true);
  });

  it('returns false for gemini', async () => {
    const { providerAutoInjectsClaudeMd } = await import('./runners.js');
    expect(providerAutoInjectsClaudeMd('gemini')).toBe(false);
  });

  it('returns false for openai', async () => {
    const { providerAutoInjectsClaudeMd } = await import('./runners.js');
    expect(providerAutoInjectsClaudeMd('openai')).toBe(false);
  });

  it('returns false for zai', async () => {
    const { providerAutoInjectsClaudeMd } = await import('./runners.js');
    expect(providerAutoInjectsClaudeMd('zai')).toBe(false);
  });

  it('returns false for deepseek', async () => {
    const { providerAutoInjectsClaudeMd } = await import('./runners.js');
    expect(providerAutoInjectsClaudeMd('deepseek')).toBe(false);
  });
});

describe('drainJsonChunks — recordLlmTokens nested guard', () => {
  beforeEach(() => {
    vi.mocked(recordLlmTokens).mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('calls recordLlmTokens for a top-level result event (no parent_tool_use_id)', async () => {
    emitStream([
      JSON.stringify({
        type: 'result',
        result: 'done',
        usage: { input_tokens: 100, output_tokens: 50, cache_creation_input_tokens: 10, cache_read_input_tokens: 5 },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordLlmTokens)).toHaveBeenCalledOnce();
    expectCalledWith(recordLlmTokens, 115, 50, 10, 5);
  });

  it('does NOT call recordLlmTokens for a nested result event (parent_tool_use_id set)', async () => {
    emitStream([
      JSON.stringify({
        type: 'result',
        parent_tool_use_id: 'parent-id-1',
        result: 'sub-agent done',
        usage: { input_tokens: 100, output_tokens: 50 },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordLlmTokens)).not.toHaveBeenCalled();
  });
});

describe('drainJsonChunks — tool-result byte accounting (user events)', () => {
  beforeEach(() => {
    vi.mocked(recordToolResultBytes).mockClear();
    vi.mocked(spawn).mockReset();
  });

  it('calls recordToolResultBytes with the JSON byte size of tool_result content blocks', async () => {
    const block = { type: 'tool_result', tool_use_id: 'tr-1', content: 'hello world' };
    const expectedBytes = Buffer.byteLength(JSON.stringify(block));
    emitStream([
      JSON.stringify({
        type: 'user',
        message: { content: [block] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordToolResultBytes)).toHaveBeenCalledOnce();
    expectCalledWith(recordToolResultBytes, expectedBytes);
  });

  it('does NOT call recordToolResultBytes for a nested user event (parent_tool_use_id set)', async () => {
    emitStream([
      JSON.stringify({
        type: 'user',
        parent_tool_use_id: 'parent-id-nested',
        message: { content: [{ type: 'tool_result', tool_use_id: 'tr-2', content: 'sub result' }] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordToolResultBytes)).not.toHaveBeenCalled();
  });

  it('does NOT call recordToolResultBytes when user event has no message content', async () => {
    emitStream([JSON.stringify({ type: 'user' })]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordToolResultBytes)).not.toHaveBeenCalled();
  });

  it('sums bytes across multiple content blocks in one user event', async () => {
    const block1 = { type: 'tool_result', tool_use_id: 'tr-3', content: 'first' };
    const block2 = { type: 'tool_result', tool_use_id: 'tr-4', content: 'second longer result' };
    const expectedBytes = Buffer.byteLength(JSON.stringify(block1)) + Buffer.byteLength(JSON.stringify(block2));
    emitStream([
      JSON.stringify({
        type: 'user',
        message: { content: [block1, block2] },
      }),
    ]);
    await runProviderAgent('prompt', null, { value: '' });
    expect(vi.mocked(recordToolResultBytes)).toHaveBeenCalledOnce();
    expectCalledWith(recordToolResultBytes, expectedBytes);
  });
});
