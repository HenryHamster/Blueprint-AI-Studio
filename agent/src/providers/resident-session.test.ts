import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Hoisted setup ─────────────────────────────────────────────────────────────
vi.hoisted(() => {
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('child_process', () => ({
  spawn: vi.fn(),
}));

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '',
  getAgentToken: vi.fn().mockReturnValue(null),
  log: vi.fn(),
  sanitize: vi.fn((s: string) => s),
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  recordNumTurns: vi.fn(),
}));

vi.mock('../token-file.js', () => ({
  writeAgentTokenFile: vi.fn(),
  jsonFieldShellFn: vi.fn(() => ''),
}));

vi.mock('../constants.js', () => ({
  PROCESS_KILL_GRACE_MS: 2000,
}));

vi.mock('../processTracking.js', () => ({
  registerChildPid: vi.fn(),
  unregisterChildPid: vi.fn(),
}));

vi.mock('./resident-stream.js', async (importOriginal) => {
  const real = await importOriginal<typeof import('./resident-stream.js')>();
  return real;
});

vi.mock('./runners.js', () => ({
  claudeHelpSupportsPartialMessages: vi.fn(() => true),
  __resetClaudeCapabilityCacheForTesting: vi.fn(),
  probeClaudeCapabilities: vi.fn(() => null),
  stripUnsupportedClaudeOptionalArgs: vi.fn((args: readonly string[]) => [...args]),
  matchesRateLimit: vi.fn((text: string) => /rate limit|weekly limit/i.test(text)),
  matchesAuthFailure: vi.fn((text: string) => /unauthorized/i.test(text)),
  getProviderRunner: vi.fn(() => ({
    provider: 'anthropic',
    displayName: 'claude',
    buildRunCommand: vi.fn(() => ({
      command: 'claude',
      args: ['-p', '--output-format', 'stream-json', '--verbose',
        '--include-partial-messages', '--dangerously-skip-permissions'],
      env: { ...process.env },
      outputMode: 'claude-stream-json',
    })),
  })),
}));

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import { spawn } from 'child_process';
import { startResidentClaudeSession } from './resident-session.js';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import { recordLlmTokens, recordNumTurns } from '../index.js';
import { encodeUserTurnBlocks } from './resident-stream.js';
import {
  probeClaudeCapabilities, stripUnsupportedClaudeOptionalArgs,
} from './runners.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

type SpawnMock = ReturnType<typeof spawn>;

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
  let stderrCb: ((d: Buffer) => void) | null = null;
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
    stderr: {
      on: vi.fn((event: string, cb: (d: Buffer) => void) => { if (event === 'data') stderrCb = cb; }),
    },
    on: vi.fn((event: string, cb: (...a: unknown[]) => void) => {
      if (event === 'close') closeCb = cb;
    }),
    kill: vi.fn(),
  };
  return {
    proc: proc as unknown as SpawnMock,
    stdinWrites,
    pushLine: (obj: unknown) => dataCb?.(Buffer.from(JSON.stringify(obj) + '\n')),
    pushRawStdout: (text: string) => dataCb?.(Buffer.from(text)),
    pushStderr: (text: string) => stderrCb?.(Buffer.from(text)),
    triggerClose: (code = 0) => { proc.exitCode = code; closeCb?.(code, null); },
  };
}

const STRATEGIC_CREATE = './strategic create-direction \'{}\'';

describe('startResidentClaudeSession', () => {
  beforeEach(() => {
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
  it('applies the Claude optional-flag compatibility gate before resident spawn', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(new Set(['--effort']));
    vi.mocked(stripUnsupportedClaudeOptionalArgs).mockImplementationOnce((args) => args.filter((arg) => arg !== '--include-partial-messages'));

    const session = startResidentClaudeSession({});
    const [, args] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];

    expect(probeClaudeCapabilities).toHaveBeenCalledWith('claude', expect.any(Object));
    expect(stripUnsupportedClaudeOptionalArgs).toHaveBeenCalledWith(expect.any(Array), expect.any(Set));
    expect(args).not.toContain('--include-partial-messages');
    expect(args).toContain('--input-format');
    session.close();
  });
  it('classifies provider rate limits from bounded stderr evidence', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    ctl.pushStderr('Error: rate limit exceeded; retry later');

    expect(session.failureCategory?.()).toBe('rate_limited');
    session.close();
  });

  it('retains an exit diagnostic when the resident dies before any turn', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    ctl.pushStderr('Error: unknown option --include-partial-messages');
    ctl.triggerClose(2);

    expect(session.exitDiagnostic?.()).toContain('exit_code=2');
    expect(session.exitDiagnostic?.()).toContain('unknown option --include-partial-messages');
  });

  it('propagates a top-level rate-limit error as a failed turn immediately', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('hello');
    ctl.pushLine({
      type: 'result',
      subtype: 'error_during_execution',
      is_error: true,
      result: 'Rate limit exceeded. Retry later.',
    });

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      failureCategory: 'rate_limited',
      resultText: 'Rate limit exceeded. Retry later.',
    });
    expect(session.failureCategory?.()).toBe('rate_limited');
    session.close();
  });

  it('treats a successful-looking weekly-limit result as a provider failure', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const events: Array<{ kind: string; text?: string }> = [];
    session.onEvent((event) => events.push(event));

    const turn = session.sendTurn('hello');
    ctl.pushLine({
      type: 'result',
      result: "You've hit your weekly limit · resets Jul 26, 9pm (UTC)",
      is_error: false,
    });

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      failureCategory: 'rate_limited',
      resultText: '',
    });
    expect(events).toContainEqual({
      kind: 'error',
      text: "You've hit your weekly limit · resets Jul 26, 9pm (UTC)",
    });
    expect(session.failureCategory?.()).toBe('rate_limited');
    session.close();
  });
  it('suppresses an assistant-text weekly-limit banner and fails the turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const events: Array<{ kind: string; text?: string }> = [];
    session.onEvent((event) => events.push(event));

    const banner = "You've hit your weekly limit · resets Jul 26, 9pm (UTC)";
    const turn = session.sendTurn('hello');
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'text', text: banner }] } });

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      failureCategory: 'rate_limited',
      resultText: '',
    });
    expect(events).not.toContainEqual({ kind: 'text', text: banner });
    expect(session.failureCategory?.()).toBe('rate_limited');
    session.close();
  });
  it('classifies an unterminated raw stdout weekly-limit banner before any turn', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    // This is the production shape observed from Claude Code at resident
    // startup: it can exit after printing plain text, with no NDJSON frame or
    // trailing newline, before the handler has sent the first user turn.
    ctl.pushRawStdout("You've hit your weekly limit · resets Jul 26, 9pm (UTC)");
    ctl.triggerClose(1);

    expect(session.failureCategory?.()).toBe('rate_limited');
  });
  it('clears transient failure evidence after a successful turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    ctl.pushStderr('warning: rate limit retry recovered');
    expect(session.failureCategory?.()).toBe('rate_limited');

    const turn = session.sendTurn('healthy turn');
    ctl.pushLine({ type: 'result', result: 'done', is_error: false });
    await expect(turn).resolves.toMatchObject({ providerFailed: false });
    expect(session.failureCategory?.()).toBeNull();

    ctl.triggerClose(1);
    expect(session.failureCategory?.()).toBeNull();
  });



  it('ignores a nested sub-agent error when the parent turn succeeds', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('parent turn');
    ctl.pushLine({
      type: 'result',
      parent_tool_use_id: 'tool-subagent',
      is_error: true,
      result: 'nested rate limit exceeded',
    });
    ctl.pushLine({ type: 'result', result: 'parent succeeded', is_error: false });

    await expect(turn).resolves.toMatchObject({
      providerFailed: false,
      failureCategory: null,
      resultText: 'parent succeeded',
    });
    expect(session.failureCategory?.()).toBeNull();
    session.close();
  });

  it('ignores a nested type:error event for parent provider classification', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('parent turn');
    ctl.pushLine({
      type: 'error',
      parent_tool_use_id: 'tool-subagent-error',
      error: { message: 'nested rate limit exceeded' },
    });
    ctl.pushLine({ type: 'result', result: 'parent succeeded', is_error: false });

    await expect(turn).resolves.toMatchObject({
      providerFailed: false,
      failureCategory: null,
      resultText: 'parent succeeded',
    });
    expect(session.failureCategory?.()).toBeNull();
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
    expect(r.accumulatedTurnText).toBe('hi back');
    expect(r.interrupted).toBe(false);
    expect(events.filter((e) => e.kind === 'text').map((e) => e.text)).toEqual(['hi ', 'back']);
    expect(session.sessionId()).toBe('sess-1');
    session.close();
  });

  it('keeps streamed marker text distinct when a tool call precedes a non-empty markerless result', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('advance');
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'TICK-VERDICT: ADVANCED — streamed\n' } } });
    ctl.pushLine({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: './strategic create-direction' } }] } });
    ctl.pushLine({ type: 'result', result: 'closing tool summary' });

    await expect(turn).resolves.toMatchObject({
      resultText: 'closing tool summary',
      accumulatedTurnText: 'TICK-VERDICT: ADVANCED — streamed\n',
      sawToolUse: true,
    });
    session.close();
  });

  it('keeps the existing empty-final-result fallback while exposing accumulated text', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('advance');
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'streamed fallback' } } });
    ctl.pushLine({ type: 'result', result: '' });

    await expect(turn).resolves.toMatchObject({
      resultText: 'streamed fallback',
      accumulatedTurnText: 'streamed fallback',
    });
    session.close();
  });

  it('sendTurn with content blocks invokes encodeUserTurnBlocks and writes multimodal NDJSON', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const blocks: Array<Record<string, unknown>> = [
      { type: 'text', text: 'check this image' },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } },
    ];

    const turn = session.sendTurn(blocks);
    // The first stdin write must match the expected NDJSON from encodeUserTurnBlocks.
    expect(ctl.stdinWrites).toHaveLength(1);
    expect(ctl.stdinWrites[0]).toBe(encodeUserTurnBlocks(blocks));

    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'got it' } } });
    ctl.pushLine({ type: 'result', result: 'got it' });
    await turn;
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

  it('records num_turns from the result event', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(recordNumTurns).mockClear();
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('q');
    ctl.pushLine({ type: 'result', result: 'a', num_turns: 6 });
    await turn;
    expect(recordNumTurns).toHaveBeenCalledWith(6);
    session.close();
  });

  it('only records the delta numTurns between consecutive result events', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(recordNumTurns).mockClear();
    const session = startResidentClaudeSession({});

    // Turn 1: cumulative num_turns=3 → delta=3 (first result)
    const turn1 = session.sendTurn('q1');
    ctl.pushLine({ type: 'result', result: 'a', num_turns: 3 });
    await turn1;
    expect(recordNumTurns).toHaveBeenNthCalledWith(1, 3);

    // Turn 2: cumulative num_turns=5 → delta=2 (the increment, not the full 5)
    const turn2 = session.sendTurn('q2');
    ctl.pushLine({ type: 'result', result: 'b', num_turns: 5 });
    await turn2;
    expect(recordNumTurns).toHaveBeenNthCalledWith(2, 2);

    // Turn 3: cumulative num_turns=6 → delta=1
    const turn3 = session.sendTurn('q3');
    ctl.pushLine({ type: 'result', result: 'c', num_turns: 6 });
    await turn3;
    expect(recordNumTurns).toHaveBeenNthCalledWith(3, 1);

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

  it('ResidentTurnResult includes turnMs >= 0 and ttftMs is null when no text arrived', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('tool-only turn');
    // Emit only a tool_use (no text), then a result.
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_1', name: 'Bash', input: { command: 'echo hi' } }] },
    });
    ctl.pushLine({ type: 'result', result: '' });

    const r = await turn;
    expect(r.turnMs).toBeGreaterThanOrEqual(0);
    expect(r.ttftMs).toBeNull(); // no text → no TTFT
    expect(r.toolCallCount).toBe(1);
    session.close();
  });

  it('ResidentTurnResult includes positive ttftMs when text arrives before settle', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('text turn');
    // Advance time a bit before the first text token arrives.
    await vi.advanceTimersByTimeAsync(50);
    ctl.pushLine({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: 'hello' } } });
    await vi.advanceTimersByTimeAsync(20);
    ctl.pushLine({ type: 'result', result: 'hello' });

    const r = await turn;
    expect(r.ttftMs).not.toBeNull();
    expect(typeof r.ttftMs).toBe('number');
    const ttftMs = r.ttftMs as number;
    expect(ttftMs).toBeGreaterThanOrEqual(0);
    expect(r.turnMs).toBeGreaterThanOrEqual(ttftMs);
    expect(r.toolCallCount).toBe(0);
    session.close();
  });
});
