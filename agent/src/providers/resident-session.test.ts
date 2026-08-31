import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import path from 'path';

// ── Hoisted setup ─────────────────────────────────────────────────────────────
vi.hoisted(() => {
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('child_process', () => ({
  spawn: vi.fn(),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn(() => { throw new Error('ENOENT'); }),
    mkdirSync: vi.fn(),
    writeFileSync: vi.fn(),
    mkdtempSync: vi.fn(() => '/tmp/blueprint-resident-codex-factory'),
    chmodSync: vi.fn(),
    rmSync: vi.fn(),
  },
  readFileSync: vi.fn(() => { throw new Error('ENOENT'); }),
  mkdirSync: vi.fn(),
  writeFileSync: vi.fn(),
}));

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '',
  getAgentToken: vi.fn().mockReturnValue(null),
  log: vi.fn(),
  sanitize: vi.fn((s: string) => s),
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  recordProviderReportedUsage: vi.fn(),
  recordNumTurns: vi.fn(),
}));

vi.mock('../token-file.js', () => ({
  writeAgentTokenFile: vi.fn(),
  jsonFieldShellFn: vi.fn(() => ''),
}));

vi.mock('../constants.js', () => ({
  DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS: 15 * 60_000,
  PROCESS_KILL_GRACE_MS: 2000,
  OUTPUT_TAIL_AFTER_CAP_BYTES: 2_000_000,
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
  syncAgentHelpersToPath: vi.fn(() => '/tmp/blueprint-agent-bin-test'),
  claudeHelpSupportsPartialMessages: vi.fn(() => true),
  __resetClaudeCapabilityCacheForTesting: vi.fn(),
  probeClaudeCapabilities: vi.fn(() => null),
  stripUnsupportedClaudeOptionalArgs: vi.fn((args: readonly string[]) => [...args]),
  TextOnlyProviderUnavailableError: class extends Error {},
  buildIsolatedTextOnlyMemoryCommand: vi.fn(),
  runIsolatedTextOnlyMemory: vi.fn(),
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
import fs from 'fs';
import { startResidentClaudeSession } from './resident-session.js';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import {
  getAgentToken, normalizeClaudeConfigContent, recordLlmTokens, recordProviderReportedUsage,
  recordNumTurns,
} from '../index.js';
import { writeAgentTokenFile } from '../token-file.js';
import { encodeUserTurnBlocks } from './resident-stream.js';
import {
  getProviderRunner, probeClaudeCapabilities, stripUnsupportedClaudeOptionalArgs,
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
      on: vi.fn(),
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
    vi.mocked(fs.readFileSync).mockReset();
    vi.mocked(fs.readFileSync).mockImplementation(() => { throw new Error('ENOENT'); });
    vi.mocked(fs.mkdirSync).mockReset();
    vi.mocked(fs.writeFileSync).mockReset();
    vi.mocked(normalizeClaudeConfigContent).mockReset();
    vi.mocked(normalizeClaudeConfigContent).mockImplementation((_path, content) => content);
    vi.mocked(probeClaudeCapabilities).mockReset();
    vi.mocked(probeClaudeCapabilities).mockReturnValue(new Set([
      '--input-format', '--append-system-prompt', '--mcp-config',
      '--strict-mcp-config', '--allowedTools',
    ]));
    vi.mocked(registerChildPid).mockClear();
    vi.mocked(unregisterChildPid).mockClear();
    vi.mocked(getAgentToken).mockReset();
    vi.mocked(getAgentToken).mockReturnValue('');
    vi.mocked(writeAgentTokenFile).mockClear();
    vi.mocked(writeAgentTokenFile).mockReturnValue(true);
    vi.spyOn(process, 'kill').mockImplementation((_pid, signal) => {
      if (signal === 0) throw Object.assign(new Error('gone'), { code: 'ESRCH' });
      return true;
    }); // never signal a real pid in tests
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

  it('selects the Codex Resident adapter for a qualified OpenAI runner', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(getProviderRunner).mockReturnValueOnce({
      provider: 'openai',
      authMode: 'oauth',
      displayName: 'OpenAI Codex',
      buildRunCommand: vi.fn(() => ({
        command: 'codex',
        args: ['exec', '--skip-git-repo-check', '--json', '--dangerously-bypass-approvals-and-sandbox', '-m', 'gpt-5.6-sol'],
        env: { CODEX_HOME: '/codex-home', ORCHESTRATOR_URL: 'http://blueprint-api:8000' },
        outputMode: 'codex-jsonl',
        timeoutMs: 900_000,
        promptInput: 'stdin',
        displayName: 'OpenAI Codex',
      })),
    } as never);
    vi.mocked(getAgentToken).mockReturnValueOnce('fresh-codex-token');

    const session = startResidentClaudeSession({
      workspace: '/workspace',
      disallowedTools: ['Write', 'Edit', 'NotebookEdit'],
    });
    const turn = session.sendTurn('hello Codex');
    const [, args] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];
    expect(args[0]).toBe('exec');
    expect(args).toContain('--json');
    expect(args).not.toContain('--input-format');
    expect(args).toContain('default_permissions="resident-read-only"');
    expect(writeAgentTokenFile).toHaveBeenCalledWith('fresh-codex-token');
    expect(vi.mocked(writeAgentTokenFile).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(spawn).mock.invocationCallOrder[0]);
    session.close();
    await vi.advanceTimersByTimeAsync(14_000);
    await expect(turn).resolves.toMatchObject({ processGone: true });
  });

  it('refuses a Codex Resident before spawn when the current task token file cannot be seeded', () => {
    vi.mocked(getProviderRunner).mockReturnValueOnce({
      provider: 'openai',
      authMode: 'oauth',
      displayName: 'OpenAI Codex',
      buildRunCommand: vi.fn(() => ({
        command: 'codex',
        args: ['exec', '--json', '-m', 'gpt-5.6-sol'],
        env: { CODEX_HOME: '/codex-home', ORCHESTRATOR_URL: 'http://blueprint-api:8000' },
        outputMode: 'codex-jsonl',
      })),
    } as never);
    vi.mocked(getAgentToken).mockReturnValueOnce('fresh-codex-token');
    vi.mocked(writeAgentTokenFile).mockReturnValueOnce(false);

    expect(() => startResidentClaudeSession({
      workspace: '/workspace',
      disallowedTools: ['Write', 'Edit', 'NotebookEdit'],
    })).toThrow(/requires a current task token file/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('materializes Claude workspace trust before spawning a Resident', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(normalizeClaudeConfigContent).mockReturnValueOnce('{"trusted":true}');

    const session = startResidentClaudeSession({
      workspace: '/workspace/agent',
      env: { HOME: '/resident-home' },
    });

    const configPath = path.join('/resident-home', '.claude.json');
    expect(normalizeClaudeConfigContent).toHaveBeenCalledWith(
      configPath, '{}', '/workspace/agent',
    );
    expect(fs.mkdirSync).toHaveBeenCalledWith(path.dirname(configPath), {
      recursive: true,
      mode: 0o700,
    });
    expect(fs.writeFileSync).toHaveBeenCalledWith(
      configPath, '{"trusted":true}',
      { encoding: 'utf8', mode: 0o600 },
    );
    session.close();
  });

  it('applies the Claude optional-flag compatibility gate before resident spawn', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(
      new Set(['--input-format', '--effort']),
    );
    vi.mocked(stripUnsupportedClaudeOptionalArgs).mockImplementationOnce((args) => args.filter((arg) => arg !== '--include-partial-messages'));

    const session = startResidentClaudeSession({});
    const [, args] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];

    expect(probeClaudeCapabilities).toHaveBeenCalledWith('claude', expect.any(Object));
    expect(stripUnsupportedClaudeOptionalArgs).toHaveBeenCalledWith(expect.any(Array), expect.any(Set));
    expect(args).not.toContain('--include-partial-messages');
    expect(args).toContain('--input-format');
    session.close();
  });
  it('appends the Resident identity without replacing Claude built-in instructions', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      appendSystemPrompt: 'canonical resident identity',
    });
    const [, args] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];
    expect(args).toEqual(expect.arrayContaining([
      '--append-system-prompt', 'canonical resident identity',
    ]));
    expect(args).not.toContain('--system-prompt');
    session.close();
  });

  it('refuses a known Claude CLI without append-system-prompt support', () => {
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(new Set(['--input-format']));
    expect(() => startResidentClaudeSession({
      appendSystemPrompt: 'canonical resident identity',
    })).toThrow(/--append-system-prompt/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('allows foreground Fast without append support while identity-bound Workday stays fail-closed', () => {
    vi.mocked(probeClaudeCapabilities).mockReturnValue(
      new Set(['--input-format']),
    );
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const fast = startResidentClaudeSession({
      foregroundLaunch: {
        command: 'claude',
        args: [
          '-p', '--output-format', 'stream-json', '--verbose',
          '--input-format', 'stream-json',
        ],
        env: { PATH: '/foreground/bin', HOME: '/foreground/home' },
        cwd: '/foreground',
      },
    });
    const [, fastArgs] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];
    expect(fastArgs).toContain('--input-format');
    expect(fastArgs).not.toContain('--append-system-prompt');
    fast.close();

    expect(() => startResidentClaudeSession({
      appendSystemPrompt: 'canonical resident identity',
    })).toThrow(/--append-system-prompt/);
    expect(spawn).toHaveBeenCalledTimes(1);
  });

  it('refuses a known Claude CLI without streaming input support', () => {
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(new Set(['--effort']));
    expect(() => startResidentClaudeSession({})).toThrow(/--input-format/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('refuses resident launch when Claude capabilities cannot be proven', () => {
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(null);
    expect(() => startResidentClaudeSession({})).toThrow(/verified Claude --input-format/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('probes and spawns with the same effective environment', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      env: { PATH: '/resident/claude/bin', CLAUDE_CONFIG_DIR: '/resident/config' },
    });
    const probedEnv = vi.mocked(probeClaudeCapabilities).mock.calls[0]?.[1];
    const spawnOptions = vi.mocked(spawn).mock.calls[0]?.[2] as { env?: NodeJS.ProcessEnv };
    expect(probedEnv).toMatchObject({
      PATH: '/resident/claude/bin', CLAUDE_CONFIG_DIR: '/resident/config',
    });
    expect(spawnOptions.env).toEqual(probedEnv);
    session.close();
  });

  it('launches the receipt-bound Workday reply tool through MCP when configured', () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      replyTool: { mcpConfigPath: '/tmp/workday-reply-mcp.json' },
    });
    const [, args] = vi.mocked(spawn).mock.calls[0] as unknown as [string, string[]];
    expect(args).toEqual(expect.arrayContaining([
      '--mcp-config', '/tmp/workday-reply-mcp.json',
      '--strict-mcp-config',
      '--allowedTools', 'mcp__workday__reply_to_operator',
    ]));
    session.close();
  });

  it('refuses a known Claude CLI that cannot isolate the reply MCP config', () => {
    vi.mocked(probeClaudeCapabilities).mockReturnValueOnce(
      new Set(['--input-format', '--mcp-config', '--allowedTools']),
    );

    expect(() => startResidentClaudeSession({
      replyTool: { mcpConfigPath: '/tmp/workday-reply-mcp.json' },
    })).toThrow(/--strict-mcp-config/);
    expect(spawn).not.toHaveBeenCalled();
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

  it('turns a structured terminal 403 into an immediate dead-auth account signal', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('hello');
    ctl.pushLine({
      type: 'result',
      subtype: 'success',
      terminal_reason: 'api_error',
      api_error_status: 403,
      result: 'Your organization has disabled Claude subscription access for Claude Code',
    });

    const result = await turn;
    expect(result).toMatchObject({
      providerFailed: true,
      failureCategory: 'dead_auth',
    });
    expect(result.failureDetail).toContain('api_error_status=403');
    expect(session.failureCategory?.()).toBe('dead_auth');
    expect(session.isAlive()).toBe(true);
    session.close();
  });

  it('retains a diagnostic for a generic top-level provider error while the child stays alive', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});

    const turn = session.sendTurn('hello');
    ctl.pushLine({
      type: 'result',
      subtype: 'error_during_execution',
      is_error: true,
      result: '',
    });

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      failureCategory: null,
      failureDetail: 'provider returned a top-level error result without diagnostic text',
    });
    expect(session.isAlive()).toBe(true);
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

  it('counts long-running nested sub-agent output as parent-turn activity', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      turnTimeoutMs: 7 * 60_000,
      hardTurnTimeoutMs: 15 * 60_000,
    });
    const turn = session.sendTurn('parent delegates a long bounded audit');
    let lastActivity = session.activeTurnProgress?.()?.lastActivityAtMs ?? 0;

    for (let minute = 1; minute <= 7; minute += 1) {
      await vi.advanceTimersByTimeAsync(60_000);
      ctl.pushLine({
        type: 'assistant',
        parent_tool_use_id: 'task-long-audit',
        message: {
          content: [{
            type: 'tool_use',
            id: `nested-tool-${minute}`,
            name: 'Read',
            input: { path: `/private/path/${minute}` },
          }],
        },
      });
      const currentActivity = session.activeTurnProgress?.()?.lastActivityAtMs ?? 0;
      expect(currentActivity).toBeGreaterThan(lastActivity);
      lastActivity = currentActivity;
    }

    ctl.pushLine({ type: 'result', result: 'parent succeeded', is_error: false });
    const result = await turn;
    expect(result).toMatchObject({
      timedOut: false,
      hardTimedOut: false,
      resultText: 'parent succeeded',
      toolCallCount: 7,
    });
    expect(result.trace?.filter(event => event.kind === 'tool')).toHaveLength(7);
    expect(JSON.stringify(result.trace)).not.toContain('/private/path/');
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

    const result = await turn;
    expect(result).toMatchObject({
      providerFailed: false,
      failureCategory: null,
      resultText: 'parent succeeded',
    });
    expect(result.trace).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'error', label: 'nested_error', isError: true }),
    ]));
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
    const cases = [
      { id: 'success', content: '{"status":"ok","op":"set_memory","doc_id":"d-1"}', applied: true },
      { id: 'refused', content: '{"status":"refused","applied":false,"op":"set_memory","http_status":409,"reason":"stale head"}', applied: false },
      { id: 'missing', content: null, applied: false },
    ];
    for (const testCase of cases) {
      const ctl = makeControllableChild();
      vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
      const session = startResidentClaudeSession({});
      const turn = session.sendTurn('advance');
      const toolUseId = `toolu_${testCase.id}`;
      ctl.pushLine({
        type: 'assistant',
        message: { content: [{
          type: 'tool_use', id: toolUseId, name: 'Bash',
          input: { command: './strategic set-memory --json \'{"body":"exact"}\'' },
        }] },
      });
      if (testCase.content !== null) {
        ctl.pushLine({
          type: 'user',
          message: { content: [{
            type: 'tool_result', tool_use_id: toolUseId,
            content: testCase.content, is_error: false,
          }] },
        });
      }
      ctl.pushLine({ type: 'result', result: 'done' });
      const r = await turn;
      expect(r.sawToolUse).toBe(true);
      expect(r.appliedStrategicOp).toBe(testCase.applied);
      session.close();
    }
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

  it('interrupt uses SIGINT without writing an unsupported control frame', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('long task');
    session.interrupt();
    expect(ctl.stdinWrites.some((w) => w.includes('control_request'))).toBe(false);
    // `process.kill` is a host method; Vitest only inspects the mock here and
    // never calls the unbound reference.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(process.kill).toHaveBeenCalledWith(4242, 'SIGINT');
    ctl.pushLine({ type: 'result', result: 'stopped' });
    const r = await turn;
    expect(r.interrupted).toBe(true);
    session.close();
  });

  it('closes a provider that never returns after cooperative interrupt', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      turnTimeoutMs: 60_000,
      hardTurnTimeoutMs: 10 * 60_000,
    });
    const turn = session.sendTurn('provider wedge');
    let settled = false;
    void turn.then(() => { settled = true; });

    session.interrupt();
    await vi.advanceTimersByTimeAsync(9_999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);

    const result = await turn;
    expect(result.interrupted).toBe(true);
    expect(result.timedOut).toBe(false);
    expect(result.processGone).toBe(true);
    expect(session.isAlive()).toBe(false);
    // The deadline tears down the detached process group rather than leaving a
    // wedged child eligible for reuse by the outer preemption coordinator.
    // `process.kill` is mocked above; this assertion never calls the unbound
    // host method.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    expect(process.kill).toHaveBeenCalledWith(-4242, 'SIGTERM');
    expect(settled).toBe(true);
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
    vi.mocked(recordProviderReportedUsage).mockClear();
    const session = startResidentClaudeSession({});
    const turn = session.sendTurn('q');
    ctl.pushLine({
      type: 'result', result: 'a',
      usage: {
        input_tokens: 50,
        output_tokens: 20,
        cache_creation_input_tokens: 10,
        cache_read_input_tokens: 5,
      },
    });
    await turn;
    // The normalized path folds in cache classes: 50 + 10 + 5 = 65.
    expect(recordLlmTokens).toHaveBeenCalledWith(65, 20, 10, 5);
    // The reconciliation path preserves the provider's raw four-field split.
    expect(recordProviderReportedUsage).toHaveBeenCalledWith(50, 20, 10, 5);
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

  it('flags sentOperatorMessage only after the native reply MCP tool succeeds', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ replyTool: { mcpConfigPath: '/tmp/workday-mcp.json' } });
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_reply', name: 'mcp__workday__reply_to_operator', input: { content: 'here you go' } }] },
    });
    // Only the exact MCP acceptance receipt confirms staging.
    ctl.pushLine({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_reply', content: 'Workday reply accepted for delivery.', is_error: false }] },
    });
    ctl.pushLine({ type: 'result', result: 'done' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(true);
    expect(r.operatorReply).toEqual({ type: 'NOTE', content: 'here you go' });
    session.close();
  });

  it('does not count an unarmed native reply result as delivered', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ replyTool: { mcpConfigPath: '/tmp/workday-mcp.json' } });
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_unarmed', name: 'mcp__workday__reply_to_operator', input: { content: 'not deliverable' } }] },
    });
    ctl.pushLine({
      type: 'user',
      message: { content: [{
        type: 'tool_result', tool_use_id: 'toolu_unarmed',
        content: 'No active operator turn is armed. Nothing was delivered.', is_error: false,
      }] },
    });
    ctl.pushLine({ type: 'result', result: 'done' });
    await expect(turn).resolves.toMatchObject({ sentOperatorMessage: false, operatorReply: null });
    session.close();
  });

  it('does NOT treat a successful ./messages send as a Workday operator reply', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ replyTool: { mcpConfigPath: '/tmp/workday-mcp.json' } });
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_bad', name: 'Bash', input: { command: "./messages send 'not a reply'" } }] },
    });
    ctl.pushLine({
      type: 'user',
      message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_bad', content: 'ok', is_error: false }] },
    });
    ctl.pushLine({ type: 'result', result: 'tried' });
    const r = await turn;
    expect(r.sentOperatorMessage).toBe(false);
    expect(r.operatorReply).toBeNull();
    session.close();
  });

  it('does NOT flag sentOperatorMessage when native reply has no confirming tool_result', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ replyTool: { mcpConfigPath: '/tmp/workday-mcp.json' } });
    const turn = session.sendTurn('reply to operator');
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_x', name: 'mcp__workday__reply_to_operator', input: { content: 'unconfirmed' } }] },
    });
    // Turn ends before any tool_result — unconfirmed, so the handler rejects it.
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

  it('exposes meaningful progress and retains bounded head-plus-tail terminal trace evidence', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ hardTurnTimeoutMs: 15 * 60_000 });
    const turn = session.sendTurn('long private consultation');
    const started = session.activeTurnProgress?.();
    expect(started).toMatchObject({ toolCallCount: 0 });

    for (let index = 0; index < 140; index += 1) {
      await vi.advanceTimersByTimeAsync(10);
      ctl.pushLine({
        type: 'assistant',
        message: {
          content: [{
            type: 'tool_use',
            id: `tool-${index}`,
            name: `Tool${index}`,
            input: {},
          }],
        },
      });
    }
    const progressed = session.activeTurnProgress?.();
    expect(progressed?.toolCallCount).toBe(140);
    expect(progressed?.lastActivityAtMs).toBeGreaterThan(started?.lastActivityAtMs ?? 0);

    ctl.pushLine({
      type: 'result',
      subtype: 'error_during_execution',
      is_error: true,
      terminal_reason: 'aborted_streaming',
      result: 'bounded provider output tail',
    });
    const result = await turn;
    expect(result.providerTerminalReason).toBe('aborted_streaming');
    expect(result.providerOutputTail).toContain('terminal_reason=aborted_streaming');
    expect(result.traceTruncated).toBe(true);
    expect(result.trace).toHaveLength(128);
    expect(result.trace?.[0]?.label).toBe('Tool0');
    expect(result.trace?.[31]?.label).toBe('Tool31');
    expect(result.trace?.at(-1)).toMatchObject({
      kind: 'result',
      label: 'aborted_streaming',
      isError: true,
    });
    session.close();
  });


  it('honors a per-turn hard deadline despite continuous streamed output', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({
      turnTimeoutMs: 60_000,
      hardTurnTimeoutMs: 60_000,
    });
    const turn = session.sendTurn('operator turn that keeps polling', { hardTimeoutMs: 5_000 });

    // These deltas continuously re-arm the inactivity watchdog. They must not
    // extend the operator turn's separate wall-clock deadline.
    for (let i = 0; i < 4; i += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
      ctl.pushLine({
        type: 'stream_event',
        event: { type: 'content_block_delta', delta: { type: 'text_delta', text: `progress-${i}` } },
      });
    }

    await vi.advanceTimersByTimeAsync(8_000);
    const result = await turn;
    expect(result.hardTimedOut).toBe(true);
    expect(result.processGone).toBe(true);
    expect(session.isAlive()).toBe(false);
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

  it('does not mis-attribute a force-settled turn’s late native reply to the NEXT turn', async () => {
    const ctl = makeControllableChild();
    vi.mocked(spawn).mockImplementationOnce(() => ctl.proc);
    const session = startResidentClaudeSession({ turnTimeoutMs: 20_000, hardTurnTimeoutMs: 10 * 60_000 });

    const turnA = session.sendTurn('A');
    await vi.advanceTimersByTimeAsync(41_000);
    const rA = await turnA;
    expect(rA.timedOut).toBe(true);

    const turnB = session.sendTurn('B');
    // A's late confirmed native reply (tool_use + successful tool_result) must
    // NOT flip B.sentOperatorMessage — it was A's reply, not B's.
    ctl.pushLine({
      type: 'assistant',
      message: { content: [{ type: 'tool_use', id: 'toolu_A', name: 'mcp__workday__reply_to_operator', input: { content: 'A reply' } }] },
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
