import { EventEmitter } from 'events';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('child_process', async (importOriginal) => ({
  ...await importOriginal<typeof import('child_process')>(),
  spawn: vi.fn(),
}));
vi.mock('../processTracking.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../processTracking.js')>(),
  registerChildPid: vi.fn(),
  unregisterChildPid: vi.fn(),
}));
vi.mock('../index.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../index.js')>(),
  WORKSPACE: '/workspace',
  log: vi.fn(),
  recordLlmCallStart: vi.fn(),
  recordLlmTokens: vi.fn(),
  recordNumTurns: vi.fn(),
  recordProviderReportedUsage: vi.fn(),
  sanitize: vi.fn((value: string) => value),
}));
vi.mock('./runners.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('./runners.js')>(),
  matchesAuthFailure: vi.fn((text: string) => /unauthorized|authentication/i.test(text)),
  matchesRateLimit: vi.fn((text: string) => /rate.?limit|usage limit/i.test(text)),
}));

import { spawn } from 'child_process';
import { unregisterChildPid } from '../processTracking.js';
import { startResidentCodexSession } from './resident-codex-session.js';
import type { ProviderRunner, RunnerCommand } from './runners.js';

const realSetImmediate = setImmediate;

class FakeChild extends EventEmitter {
  readonly pid: number;
  readonly stdout = new EventEmitter();
  readonly stderr = new EventEmitter();
  readonly stdin = Object.assign(new EventEmitter(), { end: vi.fn() });

  constructor(pid: number) {
    super();
    this.pid = pid;
  }

  line(value: unknown): void {
    this.stdout.emit('data', Buffer.from(`${JSON.stringify(value)}\n`));
  }

  close(code = 0, signal: NodeJS.Signals | null = null): void {
    this.emit('close', code, signal);
  }
}

function requiredLatestChild(children: FakeChild[]): FakeChild {
  const child = children.at(-1);
  if (child === undefined) throw new Error('expected Codex child process');
  return child;
}

async function waitForChildCount(children: FakeChild[], count: number): Promise<void> {
  for (let attempt = 0; attempt < 50 && children.length < count; attempt += 1) {
    await new Promise<void>((resolve) => realSetImmediate(resolve));
  }
  if (children.length < count) {
    throw new Error(`expected ${String(count)} Codex child processes; found ${String(children.length)}`);
  }
}

function installKillSpy() {
  return vi.spyOn(process, 'kill').mockImplementation((_pid, signal) => {
    if (signal === 0) {
      const error = Object.assign(new Error('gone'), { code: 'ESRCH' });
      throw error;
    }
    return true;
  });
}
type KillSpy = ReturnType<typeof installKillSpy>;

const runner = {
  provider: 'openai',
  authMode: 'oauth',
  displayName: 'OpenAI Codex',
} as ProviderRunner;

const base: RunnerCommand = {
  command: 'codex',
  args: [
    'exec', '--skip-git-repo-check', '--json',
    '-m', 'gpt-5.6-sol', '-s', 'danger-full-access',
  ],
  env: {
    CODEX_HOME: '/codex-home',
    ORCHESTRATOR_URL: 'http://blueprint-api:8000',
    AGENT_TOKEN_FILE: '/run/blueprint/agent-token.jwt',
    AGENT_API_TOKEN: 'must-not-reach-codex',
    ANTHROPIC_API_KEY: 'must-not-reach-codex',
    DEEPSEEK_API_KEY: 'must-not-reach-codex',
    OPENAI_API_KEY: 'must-not-reach-oauth-codex',
    CODEX_API_KEY: 'must-not-reach-oauth-codex',
  },
  outputMode: 'codex-jsonl',
  timeoutMs: 900_000,
  promptInput: 'stdin',
  displayName: 'OpenAI Codex',
};

const readOnly = { disallowedTools: ['Write', 'Edit', 'NotebookEdit'] };

describe('startResidentCodexSession', () => {
  const children: FakeChild[] = [];
  let tempDir = '';
  let killSpy: KillSpy | null = null;

  beforeEach(() => {
    children.length = 0;
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-codex-test-'));
    const codexHome = path.join(tempDir, 'authoritative-codex-home');
    fs.mkdirSync(codexHome, { mode: 0o700 });
    fs.writeFileSync(path.join(codexHome, 'auth.json'), '{"tokens":{"access_token":"test"}}', { mode: 0o600 });
    base.env.CODEX_HOME = codexHome;
    base.command = process.execPath;
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const child = new FakeChild(5100 + children.length);
      children.push(child);
      return child as never;
    });
    killSpy = installKillSpy();
    vi.useFakeTimers();
  });

  const directIsolation = () => {
    const stateDir = path.join(tempDir, 'resident-state');
    const mcpDir = path.join(tempDir, 'resident-mcp');
    const deniedDir = path.join(tempDir, 'denied-repository');
    for (const directory of [stateDir, mcpDir, deniedDir]) {
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      fs.chmodSync(directory, 0o700);
    }
    return {
      codexIsolation: {
        agentTokenPath: path.join(mcpDir, 'agent-token.jwt'),
        readonlyPaths: [mcpDir],
        readwritePaths: [stateDir],
        denyReadPaths: [deniedDir],
        denyWritePaths: [deniedDir, mcpDir],
        persistOAuthCredential: vi.fn(() => Promise.resolve()),
      },
    };
  };

  const trustedMcpScript = (name: string): string => {
    const directory = path.join(tempDir, 'resident-mcp');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const script = path.join(directory, name);
    fs.writeFileSync(script, '#!/usr/bin/env node\n', { mode: 0o500 });
    fs.chmodSync(script, 0o500);
    return script;
  };

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    killSpy = null;
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  it('uses exact thread identity for a second exec resume turn', async () => {
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      appendSystemPrompt: 'canonical Resident identity',
    }, runner, base);

    const first = session.sendTurn('first prompt');
    expect(children[0].stdin.end).toHaveBeenCalledWith('first prompt');
    const firstArgs = vi.mocked(spawn).mock.calls[0][1] as string[];
    expect(firstArgs.slice(0, 2)).toEqual([
      '-I', expect.stringMatching(/resident_landlock_exec\.py$/),
    ]);
    const commandBoundary = firstArgs.indexOf('--');
    expect(commandBoundary).toBeGreaterThan(3);
    expect(firstArgs.slice(commandBoundary + 1, commandBoundary + 3)).toEqual([
      fs.realpathSync(process.execPath), 'exec',
    ]);
    const firstSpawnOptions = vi.mocked(spawn).mock.calls[0][2] as {
      cwd: string; env: Record<string, string>;
    };
    const scratchDir = firstSpawnOptions.env.TMPDIR;
    const policy = JSON.parse(firstArgs[firstArgs.indexOf('--policy-json') + 1]) as {
      readonly_paths: string[]; readwrite_paths: string[];
      deny_read_paths: string[]; deny_write_paths: string[];
    };
    expect(policy.readwrite_paths).toEqual([scratchDir]);
    expect(policy.readwrite_paths).not.toContain(path.join(tempDir, 'resident-state'));
    expect(policy.deny_read_paths).toContain(path.join(tempDir, 'denied-repository'));
    expect(policy.deny_write_paths).toContain(path.join(tempDir, 'resident-mcp'));
    expect(firstArgs).toContain('--ignore-user-config');
    expect(firstArgs).toContain('--strict-config');
    expect(firstArgs).not.toContain('read-only');
    expect(firstArgs).not.toContain('use_legacy_landlock');
    expect(firstArgs).toContain('shell_tool');
    expect(firstArgs).toContain('unified_exec');
    expect(firstArgs).toContain('apply_patch_freeform');
    expect(firstArgs).toContain('approval_policy="never"');
    expect(firstArgs.some((arg) => arg.startsWith('sandbox_workspace_write.'))).toBe(false);
    expect(firstArgs.some((arg) => arg.startsWith('permissions.resident-read-only.'))).toBe(false);
    expect(firstArgs).not.toContain('default_permissions="resident-read-only"');
    expect(firstArgs).not.toContain('features.network_proxy=true');
    expect(firstArgs).toContain('web_search="disabled"');
    expect(firstArgs).toContain('tools.web_search=false');
    expect(firstArgs).toContain('shell_environment_policy.inherit="none"');
    expect(scratchDir).toMatch(/\.codex-session-/);
    expect(fs.statSync(scratchDir).mode & 0o777).toBe(0o700);
    expect(firstSpawnOptions.cwd).toBe(scratchDir);
    expect(firstArgs).toContain(`shell_environment_policy.set.TMPDIR=${JSON.stringify(scratchDir)}`);
    expect(firstSpawnOptions.env.AGENT_TOKEN_FILE).toBeUndefined();
    expect(firstSpawnOptions.env.ORCHESTRATOR_URL).toBeUndefined();
    expect(firstSpawnOptions.env.AGENT_API_TOKEN).toBeUndefined();
    expect(firstSpawnOptions.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.DEEPSEEK_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.OPENAI_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.CODEX_API_KEY).toBeUndefined();
    expect(firstArgs).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(firstArgs).not.toContain('-s');
    expect(firstArgs).not.toContain('danger-full-access');
    expect(firstArgs.some((arg) => (
      arg.startsWith('developer_instructions=')
      && arg.includes('canonical Resident identity')
      && arg.includes('without native shell')
    ))).toBe(true);
    expect(firstArgs).not.toContain('resume');
    expect(firstArgs.at(-1)).toBe('-');

    children[0].line({ type: 'thread.started', thread_id: 'thread-exact-1' });
    children[0].line({ type: 'item.completed', item: { id: 'msg-1', type: 'agent_message', text: 'one' } });
    children[0].line({ type: 'turn.completed', usage: { input_tokens: 12, cached_input_tokens: 3, output_tokens: 4 } });
    children[0].close();
    await expect(first).resolves.toMatchObject({ resultText: 'one', processGone: false });

    const second = session.sendTurn('second prompt');
    const secondArgs = vi.mocked(spawn).mock.calls[1][1] as string[];
    const secondBoundary = secondArgs.indexOf('--');
    expect(secondArgs.slice(secondBoundary + 2, secondBoundary + 4)).toEqual(['exec', 'resume']);
    expect(secondArgs.slice(-2)).toEqual(['thread-exact-1', '-']);
    expect(secondArgs).not.toContain('--last');
    expect(secondArgs).not.toContain('--ephemeral');
    expect(secondArgs).not.toContain('use_legacy_landlock');
    expect(secondArgs).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(children[1].stdin.end).toHaveBeenCalledWith('second prompt');
    children[1].line({ type: 'thread.started', thread_id: 'thread-exact-1' });
    children[1].line({ type: 'item.completed', item: { id: 'msg-2', type: 'agent_message', text: 'two' } });
    children[1].line({ type: 'turn.completed' });
    children[1].close();
    await expect(second).resolves.toMatchObject({ resultText: 'two', processGone: false });
    expect(session.sessionId()).toBe('thread-exact-1');
    session.close();
    expect(fs.existsSync(scratchDir)).toBe(false);
  });

  it.each([
    { label: 'missing', resumedThreadId: null, reason: 'missing_thread_id' },
    { label: 'mismatched', resumedThreadId: 'thread-other', reason: 'thread_id_mismatch' },
  ])('revokes an accepted reply when the resumed process has a $label thread receipt', async ({
    label, resumedThreadId, reason,
  }) => {
    const mcpConfig = path.join(tempDir, `resident-resume-${label}-mcp.json`);
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: { command: process.execPath, args: [trustedMcpScript('reply.mjs')] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);

    const first = session.sendTurn('establish exact thread');
    await waitForChildCount(children, 1);
    const firstChild = requiredLatestChild(children);
    firstChild.line({ type: 'thread.started', thread_id: 'thread-resume-proof' });
    firstChild.line({ type: 'turn.completed' });
    firstChild.close();
    await expect(first).resolves.toMatchObject({ processGone: false });

    const resumed = session.sendTurn('reply on exact resumed thread');
    await waitForChildCount(children, 2);
    const resumedArgs = vi.mocked(spawn).mock.calls.at(-1)?.[1] as string[];
    expect(resumedArgs.slice(-2)).toEqual(['thread-resume-proof', '-']);
    const resumedChild = requiredLatestChild(children);
    if (resumedThreadId !== null) {
      resumedChild.line({ type: 'thread.started', thread_id: resumedThreadId });
    }
    resumedChild.line({ type: 'item.started', item: {
      id: 'mcp-resume', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      arguments: { content: 'must remain uncommitted', message_type: 'NOTE' },
    } });
    resumedChild.line({ type: 'item.completed', item: {
      id: 'mcp-resume', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      status: 'completed', error: null,
      result: {
        content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
        structured_content: { accepted: true },
      },
    } });
    resumedChild.line({ type: 'turn.completed' });
    resumedChild.close();

    await expect(resumed).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: reason,
      processGone: true,
      sentOperatorMessage: false,
      operatorReply: null,
    });
    expect(session.sessionId()).toBe('thread-resume-proof');
    expect(session.isAlive()).toBe(false);
  });

  it.each([
    {
      label: 'turn.failed',
      terminal: { type: 'turn.failed', error: { message: 'provider rejected turn' } },
      trailingCompleted: false,
      reason: 'turn_failed',
    },
    {
      label: 'top-level error followed by completion',
      terminal: { type: 'error', message: 'provider stream failed' },
      trailingCompleted: true,
      reason: 'provider_error',
    },
  ])('revokes an accepted reply after $label', async ({
    terminal, trailingCompleted, reason,
  }) => {
    const mcpConfig = path.join(tempDir, `resident-terminal-${reason}-mcp.json`);
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: { command: process.execPath, args: [trustedMcpScript('reply.mjs')] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('reply before terminal failure');
    await waitForChildCount(children, 1);
    children[0].line({ type: 'thread.started', thread_id: 'thread-terminal-failure' });
    children[0].line({ type: 'item.started', item: {
      id: 'mcp-terminal', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      arguments: { content: 'must remain uncommitted', message_type: 'NOTE' },
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'mcp-terminal', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      status: 'completed', error: null,
      result: {
        content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
        structured_content: { accepted: true },
      },
    } });
    children[0].line(terminal);
    if (trailingCompleted) children[0].line({ type: 'turn.completed' });
    children[0].close();

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: reason,
      processGone: true,
      appliedStrategicOp: false,
      sentOperatorMessage: false,
      operatorReply: null,
    });
    expect(session.isAlive()).toBe(false);
  });

  it('requires exact successful MCP receipts before acknowledging delivery', async () => {
    const mcpConfig = path.join(tempDir, 'resident-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: {
          command: process.execPath,
          args: [trustedMcpScript('reply.mjs')],
          env: {
            RESIDENT_REPLY_RECEIPT_PATH: '/workspace/receipt.json',
            RESIDENT_STAGED_REPLY_PATH: '/workspace/staged.json',
          },
        },
        call: { command: process.execPath, args: [trustedMcpScript('call.mjs')] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: {
        mcpConfigPath: mcpConfig,
        allowedTools: ['mcp__workday__reply_to_operator', 'mcp__call__emit_call_packet'],
      },
    }, runner, base);
    const turn = session.sendTurn('mutate and reply');
    await waitForChildCount(children, 1);
    const args = vi.mocked(spawn).mock.calls[0][1] as string[];
    expect(args).toContain('mcp_servers.workday.required=true');
    expect(args).toContain('mcp_servers={}');
    expect(args).toContain(`mcp_servers.workday.command=${JSON.stringify(process.execPath)}`);
    const brokerArgs = args.find((arg) => arg.startsWith('mcp_servers.workday.args='));
    expect(brokerArgs).toContain('resident_mcp_socket_client.mjs');
    expect(brokerArgs).toContain('blueprint-resident-mcp-');
    expect(args.some((arg) => arg.startsWith('mcp_servers.workday.env.'))).toBe(false);
    expect(args.join(' ')).not.toContain('/workspace/receipt.json');
    expect(args.join(' ')).not.toContain('/workspace/staged.json');
    expect(args).toContain('mcp_servers.workday.enabled=true');
    expect(args).toContain('mcp_servers.workday.enabled_tools=["reply_to_operator"]');
    expect(args).toContain('mcp_servers.call.required=true');
    expect(args).toContain('mcp_servers.call.enabled_tools=["emit_call_packet"]');
    expect(args).toContain('mcp_servers.workday.default_tools_approval_mode="approve"');
    expect(args).toContain('mcp_servers.workday.enabled_tools=["reply_to_operator"]');

    children[0].line({ type: 'thread.started', thread_id: 'thread-tools' });
    children[0].line({ type: 'item.started', item: {
      id: 'mcp-1', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      arguments: { content: 'operator answer', message_type: 'NOTE' }, status: 'in_progress',
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'mcp-1', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      status: 'completed', error: null,
      result: {
        content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
        structured_content: { accepted: true },
      },
    } });
    children[0].line({ type: 'turn.completed' });
    children[0].close();

    await expect(turn).resolves.toMatchObject({
      appliedStrategicOp: false,
      sentOperatorMessage: true,
      operatorReply: { type: 'NOTE', content: 'operator answer' },
      toolCallCount: 1,
    });
    session.close();
  });

  it('fails closed before Codex spawn when private broker socket setup fails', async () => {
    const mcpConfig = path.join(tempDir, 'resident-broker-startup-failure-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: { command: process.execPath, args: [trustedMcpScript('startup-failure.mjs')] },
      },
    }));
    const isolation = directIsolation();
    const realChmodSync = fs.chmodSync;
    vi.spyOn(fs, 'chmodSync').mockImplementation((target, mode) => {
      if (String(target).endsWith('.sock')) throw new Error('private socket chmod denied');
      realChmodSync(target, mode);
    });

    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      ...isolation,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('must not start before the broker is private');

    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'mcp_broker_failed',
      processGone: true,
      sentOperatorMessage: false,
      appliedStrategicOp: false,
    });
    expect(spawn).not.toHaveBeenCalled();
    expect(session.isAlive()).toBe(false);
    session.close();
    await expect(session.waitForIdle()).resolves.toBeUndefined();
  });

  it('keeps the task token behind the credential-free MCP socket boundary', async () => {
    vi.useRealTimers();
    const actualChildProcess = await vi.importActual<typeof import('child_process')>('child_process');
    const token = 'broker-only-task-token';
    const tokenPath = path.join(tempDir, 'task-agent-token.jwt');
    fs.writeFileSync(tokenPath, token, { mode: 0o600 });
    const sourceFixtureScript = path.resolve(
      'src/providers/__fixtures__/resident-mcp-token-echo.mjs',
    );
    const isolation = directIsolation();
    const fixtureScript = path.join(tempDir, 'resident-mcp', 'resident-mcp-token-echo.mjs');
    fs.copyFileSync(sourceFixtureScript, fixtureScript);
    fs.chmodSync(fixtureScript, 0o500);
    isolation.codexIsolation.agentTokenPath = tokenPath;
    isolation.codexIsolation.denyReadPaths = [
      ...isolation.codexIsolation.denyReadPaths,
      tokenPath,
    ];
    isolation.codexIsolation.denyWritePaths = [
      ...isolation.codexIsolation.denyWritePaths,
      tokenPath,
    ];
    const mcpConfig = path.join(tempDir, 'resident-broker-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: {
          command: process.execPath,
          args: [fixtureScript],
          env: { AGENT_TOKEN_FILE: tokenPath },
        },
      },
    }));
    vi.mocked(spawn).mockImplementation((command, args, options) => {
      if (command === process.execPath && args[0] === fixtureScript) {
        return actualChildProcess.spawn(command, args, options);
      }
      const child = new FakeChild(5100 + children.length);
      children.push(child);
      return child as never;
    });

    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      ...isolation,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('broker one bounded tool request');
    await waitForChildCount(children, 1);
    const codexArgs = vi.mocked(spawn).mock.calls.find((call) => (
      Array.isArray(call[1]) && call[1][0] === '-I'
    ))?.[1] as string[];
    const brokerArg = codexArgs.find((arg) => arg.startsWith('mcp_servers.workday.args='));
    expect(brokerArg).toBeDefined();
    if (brokerArg === undefined) throw new Error('expected Workday broker argument');
    expect(brokerArg).not.toContain(token);
    expect(brokerArg).not.toContain(tokenPath);
    const clientArgs = JSON.parse(brokerArg.slice(brokerArg.indexOf('=') + 1)) as string[];
    expect(clientArgs[0]).toMatch(/resident_mcp_socket_client\.mjs$/);
    const policy = JSON.parse(codexArgs[codexArgs.indexOf('--policy-json') + 1]) as {
      deny_read_paths: string[];
      deny_write_paths: string[];
    };
    expect(policy.deny_read_paths).toContain(fs.realpathSync(tokenPath));
    expect(policy.deny_write_paths).toContain(fs.realpathSync(tokenPath));
    expect(fs.statSync(path.dirname(clientArgs[1])).mode & 0o777).toBe(0o700);
    expect(fs.statSync(clientArgs[1]).mode & 0o777).toBe(0o600);

    const roundtrip = async (
      args: string[], request: string, beforeEnd?: () => void,
    ): Promise<void> => {
      const client = actualChildProcess.spawn(process.execPath, args, {
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      let clientStderr = '';
      client.stderr.setEncoding('utf8');
      client.stderr.on('data', (chunk: string) => { clientStderr += chunk; });
      let responseReceived = false;
      const response = new Promise<string>((resolve, reject) => {
        let buffered = '';
        client.stdout.setEncoding('utf8');
        client.stdout.on('data', (chunk: string) => {
          buffered += chunk;
          const newline = buffered.indexOf('\n');
          if (newline >= 0) {
            responseReceived = true;
            resolve(buffered.slice(0, newline));
          }
        });
        client.once('error', reject);
        client.once('close', (code) => {
          if (!responseReceived) reject(new Error(
            `socket client closed before response (${String(code)}): ${clientStderr}`,
          ));
        });
      });
      client.stdin.write(`${request}\n`);
      await expect(response).resolves.toBe(JSON.stringify({ request, token }));
      beforeEnd?.();
      client.stdin.end();
      await new Promise<void>((resolve, reject) => {
        client.once('close', (code) => {
          if (code === 0) resolve();
          else reject(new Error(`socket client exited ${String(code)}`));
        });
      });
    };

    await roundtrip(clientArgs, 'bounded-request');
    const upstreamCall = vi.mocked(spawn).mock.calls.find((call) => call[1][0] === fixtureScript);
    expect((upstreamCall?.[2] as { env: Record<string, string> }).env.AGENT_TOKEN_FILE).toBe(tokenPath);

    const codexChild = requiredLatestChild(children);
    codexChild.line({ type: 'thread.started', thread_id: 'thread-broker-boundary' });
    codexChild.line({ type: 'turn.completed' });
    codexChild.close();
    await expect(turn).resolves.toMatchObject({ processGone: false });

    const resumed = session.sendTurn('repeat only after the broker drained');
    await waitForChildCount(children, 2);
    await roundtrip(clientArgs, 'second-request');
    const resumedChild = requiredLatestChild(children);
    resumedChild.line({ type: 'thread.started', thread_id: 'thread-broker-boundary' });
    resumedChild.line({ type: 'turn.completed' });
    resumedChild.close();
    await expect(resumed).resolves.toMatchObject({ processGone: false });
    session.close();
    await expect(session.waitForIdle()).resolves.toBeUndefined();

    const heldSession = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      ...isolation,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const heldTurn = heldSession.sendTurn('fail closed when the broker does not drain');
    await waitForChildCount(children, 3);
    const heldCodexArgs = [...vi.mocked(spawn).mock.calls].reverse().find((call) => (
      Array.isArray(call[1]) && call[1][0] === '-I'
    ))?.[1] as string[];
    const heldBrokerArg = heldCodexArgs.find((arg) => arg.startsWith('mcp_servers.workday.args='));
    if (heldBrokerArg === undefined) throw new Error('expected held Workday broker argument');
    const heldClientArgs = JSON.parse(heldBrokerArg.slice(heldBrokerArg.indexOf('=') + 1)) as string[];
    await roundtrip(heldClientArgs, 'hold-open');
    const heldCodex = requiredLatestChild(children);
    heldCodex.line({ type: 'thread.started', thread_id: 'thread-broker-held' });
    heldCodex.line({ type: 'turn.completed' });
    heldCodex.close();
    await expect(heldTurn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'mcp_broker_drain_timeout',
      sentOperatorMessage: false,
      appliedStrategicOp: false,
    });
    heldSession.close();
    await expect(heldSession.waitForIdle()).resolves.toBeUndefined();

    const orderedSession = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      ...isolation,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const orderedTurn = orderedSession.sendTurn('preserve scratch until Codex is exactly gone');
    await waitForChildCount(children, 4);
    const orderedCall = [...vi.mocked(spawn).mock.calls].reverse().find((call) => (
      Array.isArray(call[1]) && call[1][0] === '-I'
    ));
    if (orderedCall === undefined) throw new Error('expected ordered Codex spawn');
    const orderedArgs = orderedCall[1] as string[];
    const orderedScratch = (orderedCall[2] as { env: Record<string, string> }).env.TMPDIR;
    const orderedBrokerArg = orderedArgs.find((arg) => arg.startsWith('mcp_servers.workday.args='));
    if (orderedBrokerArg === undefined) throw new Error('expected ordered Workday broker argument');
    const orderedClientArgs = JSON.parse(
      orderedBrokerArg.slice(orderedBrokerArg.indexOf('=') + 1),
    ) as string[];
    await roundtrip(orderedClientArgs, 'broker-closes-first', () => { orderedSession.close(); });
    expect(fs.existsSync(orderedScratch)).toBe(true);
    let orderedIdle = false;
    const orderedIdleResult = orderedSession.waitForIdle().then(() => { orderedIdle = true; });
    await Promise.resolve();
    expect(orderedIdle).toBe(false);
    const orderedCodex = requiredLatestChild(children);
    orderedCodex.close(143, 'SIGTERM');
    await expect(orderedTurn).resolves.toMatchObject({ interrupted: true, processGone: true });
    await expect(orderedIdleResult).resolves.toBeUndefined();
    expect(fs.existsSync(orderedScratch)).toBe(false);

    const failedSession = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      ...isolation,
      replyTool: {
        mcpConfigPath: mcpConfig,
        allowedTools: ['mcp__workday__reply_to_operator', 'mcp__workday__strategic_operation'],
      },
    }, runner, base);
    const failedTurn = failedSession.sendTurn('revoke acknowledgements after upstream failure');
    await waitForChildCount(children, 5);
    const failedArgs = ([...vi.mocked(spawn).mock.calls].reverse().find((call) => (
      Array.isArray(call[1]) && call[1][0] === '-I'
    ))?.[1] ?? []) as string[];
    const failedBrokerArg = failedArgs.find((arg) => arg.startsWith('mcp_servers.workday.args='));
    if (failedBrokerArg === undefined) throw new Error('expected failed Workday broker argument');
    const failedClientArgs = JSON.parse(
      failedBrokerArg.slice(failedBrokerArg.indexOf('=') + 1),
    ) as string[];
    const failedCodex = requiredLatestChild(children);
    await roundtrip(failedClientArgs, 'exit-nonzero', () => {
      failedCodex.line({ type: 'thread.started', thread_id: 'thread-broker-failed' });
      failedCodex.line({
        type: 'item.started',
        item: {
          id: 'failed-reply', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
          arguments: { content: 'must be revoked', message_type: 'NOTE' },
        },
      });
      failedCodex.line({
        type: 'item.completed',
        item: {
          id: 'failed-reply', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
          status: 'completed', error: null,
          arguments: { content: 'must be revoked', message_type: 'NOTE' },
          result: {
            content: [{ type: 'text', text: 'Workday reply accepted for delivery.' }],
            structured_content: { accepted: true },
          },
        },
      });
      failedCodex.line({
        type: 'item.started',
        item: {
          id: 'failed-strategic', type: 'mcp_tool_call', server: 'workday', tool: 'strategic_operation',
          arguments: { operation: 'set-running-update', data: {}, idempotency_key: 'failed-key' },
        },
      });
      failedCodex.line({
        type: 'item.completed',
        item: {
          id: 'failed-strategic', type: 'mcp_tool_call', server: 'workday', tool: 'strategic_operation',
          status: 'completed', error: null,
          arguments: { operation: 'set-running-update', data: {}, idempotency_key: 'failed-key' },
          result: {
            structured_content: {
              outcome: 'applied', applied: true, commit_acknowledged: true, receipt_confirmed: true,
            },
          },
        },
      });
      failedCodex.line({ type: 'turn.completed' });
      failedCodex.close();
    });
    await expect(failedTurn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'mcp_broker_failed',
      sentOperatorMessage: false,
      operatorReply: null,
      appliedStrategicOp: false,
    });
    expect(failedSession.isAlive()).toBe(false);
    failedSession.close();
    await expect(failedSession.waitForIdle()).resolves.toBeUndefined();
  });

  it.each([
    { commitAcknowledged: false, expectedApplied: false },
    { commitAcknowledged: true, expectedApplied: true },
  ])('requires an exact committed strategic result (ack=$commitAcknowledged)', async ({
    commitAcknowledged, expectedApplied,
  }) => {
    const mcpConfig = path.join(tempDir, `resident-strategic-${String(commitAcknowledged)}.json`);
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: { command: process.execPath, args: [trustedMcpScript('workday.mjs')] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: {
        mcpConfigPath: mcpConfig,
        allowedTools: ['mcp__workday__strategic_operation'],
      },
    }, runner, base);
    const turn = session.sendTurn('apply one bounded mutation');
    await waitForChildCount(children, 1);
    children[0].line({ type: 'thread.started', thread_id: `thread-strategic-${String(commitAcknowledged)}` });
    children[0].line({ type: 'item.started', item: {
      id: 'mcp-strategic', type: 'mcp_tool_call', server: 'workday', tool: 'strategic_operation',
      arguments: { operation: 'set-running-update', data: { markdown: 'exact' }, idempotency_key: 'exact-key' },
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'mcp-strategic', type: 'mcp_tool_call', server: 'workday', tool: 'strategic_operation',
      status: 'completed', error: null,
      result: {
        structured_content: {
          outcome: 'applied', applied: true, commit_acknowledged: commitAcknowledged,
          receipt_confirmed: true,
        },
      },
    } });
    children[0].line({ type: 'turn.completed' });
    children[0].close();

    await expect(turn).resolves.toMatchObject({ appliedStrategicOp: expectedApplied });
    session.close();
  });

  it('does not accept an unarmed Workday tool result as delivery', async () => {
    const mcpConfig = path.join(tempDir, 'resident-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: { workday: { command: process.execPath, args: [trustedMcpScript('reply.mjs')] } },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('reply');
    await waitForChildCount(children, 1);
    children[0].line({ type: 'thread.started', thread_id: 'thread-unarmed' });
    children[0].line({ type: 'item.started', item: {
      id: 'mcp-1', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      arguments: { content: 'not delivered' },
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'mcp-1', type: 'mcp_tool_call', server: 'workday', tool: 'reply_to_operator',
      status: 'completed', error: null,
      result: {
        content: [{ type: 'text', text: 'No active operator turn is armed.' }],
        structured_content: { accepted: false },
      },
    } });
    children[0].line({ type: 'turn.completed' });
    children[0].close();
    await expect(turn).resolves.toMatchObject({ sentOperatorMessage: false, operatorReply: null });
    session.close();
  });

  it.each(['command_execution', 'file_change', 'web_search', 'todo_list', 'computer_use'])(
    'fails closed on forbidden local item %s',
    async (itemType) => {
      const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
      const turn = session.sendTurn('do not use local tools');
      children[0].line({ type: 'thread.started', thread_id: `thread-forbidden-${itemType}` });
      children[0].line({ type: 'item.started', item: {
        id: `forbidden-${itemType}`, type: itemType, status: 'in_progress',
      } });
      children[0].close(130, 'SIGINT');

      await expect(turn).resolves.toMatchObject({
        providerFailed: true,
        providerTerminalReason: 'unexpected_local_tool',
        processGone: true,
        sentOperatorMessage: false,
        appliedStrategicOp: false,
      });
      expect(killSpy).toHaveBeenCalledWith(-5100, 'SIGINT');
      expect(session.isAlive()).toBe(false);
    },
  );

  it('fails closed when the process exits without turn.completed', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('incomplete');
    children[0].line({ type: 'thread.started', thread_id: 'thread-incomplete' });
    children[0].line({ type: 'item.completed', item: { id: 'msg', type: 'agent_message', text: 'partial' } });
    children[0].close(0);
    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'missing_turn_completed',
      processGone: true,
    });
    expect(session.isAlive()).toBe(false);
  });

  it('taints the thread and forces a cold restart after interrupt', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('ambiguous mutation');
    children[0].line({ type: 'thread.started', thread_id: 'thread-tainted' });
    session.interrupt();
    expect(killSpy).toHaveBeenCalledWith(-5100, 'SIGINT');
    children[0].close(130, 'SIGINT');
    await expect(turn).resolves.toMatchObject({ interrupted: true, processGone: true });
    expect(session.isAlive()).toBe(false);
  });

  it('rejects unsafe sandbox translations and requires the complete read-only contract', () => {
    const unsafe = { ...base, args: ['exec', '-s', 'workspace-write'] };
    expect(() => startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, unsafe))
      .toThrow(/sandbox mode workspace-write/);
    expect(() => startResidentCodexSession({ workspace: tempDir, ...directIsolation() }, runner, base))
      .toThrow(/complete read-only tool boundary/);
    const unsafeMcp = path.join(tempDir, 'unsafe-mcp.json');
    fs.writeFileSync(unsafeMcp, JSON.stringify({
      mcpServers: {
        workday: {
          command: process.execPath,
          env: { RESIDENT_API_TOKEN: 'must-never-enter-argv' },
        },
      },
    }));
    expect(() => startResidentCodexSession({
      ...readOnly,
      workspace: tempDir, ...directIsolation(),
      replyTool: { mcpConfigPath: unsafeMcp },
    }, runner, base)).toThrow(/env key RESIDENT_API_TOKEN is not allowed/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('passes only the selected API-key auth mode to the Codex parent', async () => {
    const apiRunner = { ...runner, authMode: 'api_key' } as ProviderRunner;
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, apiRunner, base);
    const turn = session.sendTurn('api-key turn');
    const spawnOptions = vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> };
    expect(spawnOptions.env.CODEX_HOME).toBeUndefined();
    expect(spawnOptions.env.OPENAI_API_KEY).toBe('must-not-reach-oauth-codex');
    expect(spawnOptions.env.CODEX_API_KEY).toBe('must-not-reach-oauth-codex');
    children[0].line({ type: 'thread.started', thread_id: 'thread-api-key' });
    children[0].line({ type: 'turn.completed' });
    children[0].close();
    await expect(turn).resolves.toMatchObject({ processGone: false });
    session.close();
  });

  it('fences OAuth persistence before settlement and adopts the authoritative CAS winner', async () => {
    const sourceAuth = path.join(base.env.CODEX_HOME, 'auth.json');
    let releasePersistence: (() => void) | undefined;
    let persistenceEntered: (() => void) | undefined;
    const persistenceGate = new Promise<void>((resolve) => { releasePersistence = resolve; });
    const entered = new Promise<void>((resolve) => { persistenceEntered = resolve; });
    const isolation = directIsolation();
    isolation.codexIsolation.persistOAuthCredential = vi.fn(async () => {
      persistenceEntered?.();
      await persistenceGate;
      fs.writeFileSync(sourceAuth, '{"tokens":{"access_token":"winner"}}', { mode: 0o600 });
    });
    const session = startResidentCodexSession({
      ...readOnly, workspace: tempDir, ...isolation,
      turnTimeoutMs: 50, hardTurnTimeoutMs: 100,
    }, runner, base);
    const turn = session.sendTurn('rotate OAuth');
    const spawnOptions = vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> };
    const scratchAuth = path.join(spawnOptions.env.CODEX_HOME, 'auth.json');
    fs.writeFileSync(scratchAuth, '{"tokens":{"access_token":"rotated"}}', { mode: 0o600 });
    let settled = false;
    void turn.then(() => { settled = true; });

    children[0].line({ type: 'thread.started', thread_id: 'thread-oauth' });
    children[0].line({ type: 'turn.completed' });
    children[0].close();
    await entered;
    let idleSettled = false;
    const idle = session.waitForIdle().then(() => { idleSettled = true; });
    session.close();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(settled).toBe(false);
    expect(idleSettled).toBe(false);
    expect(killSpy).not.toHaveBeenCalled();
    expect(fs.existsSync(scratchAuth)).toBe(true);
    expect(fs.readFileSync(sourceAuth, 'utf-8')).toContain('rotated');

    releasePersistence?.();
    await expect(turn).resolves.toMatchObject({
      providerFailed: false,
      providerTerminalReason: 'completed',
      processGone: false,
    });
    expect(isolation.codexIsolation.persistOAuthCredential).toHaveBeenCalledTimes(1);
    expect(fs.readFileSync(sourceAuth, 'utf-8')).toContain('winner');
    expect(fs.existsSync(scratchAuth)).toBe(false);
    await expect(idle).resolves.toBeUndefined();
  });

  it('retains private OAuth state when authoritative persistence fails', async () => {
    const sourceAuth = path.join(base.env.CODEX_HOME, 'auth.json');
    const realRename = fs.renameSync.bind(fs);
    vi.spyOn(fs, 'renameSync').mockImplementation((from, to) => {
      if (to === sourceAuth) throw new Error('authoritative credential write denied');
      realRename(from, to);
    });
    const session = startResidentCodexSession({
      ...readOnly, workspace: tempDir, ...directIsolation(),
    }, runner, base);
    const turn = session.sendTurn('rotate OAuth and fail persistence');
    const spawnOptions = vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> };
    const scratchAuth = path.join(spawnOptions.env.CODEX_HOME, 'auth.json');
    fs.writeFileSync(scratchAuth, '{"tokens":{"access_token":"only-new-token"}}', { mode: 0o600 });

    children[0].line({ type: 'thread.started', thread_id: 'thread-oauth-failure' });
    children[0].line({ type: 'turn.completed' });
    children[0].close();
    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'oauth_rotation_failed',
      processGone: true,
    });
    expect(session.privateRuntimeCleanupSafe?.()).toBe(false);
    expect(fs.readFileSync(scratchAuth, 'utf-8')).toContain('only-new-token');
    session.close();
    await expect(session.waitForIdle()).resolves.toBeUndefined();
    expect(fs.existsSync(scratchAuth)).toBe(true);
  });

  it('bounds an interrupt through SIGKILL when the child emits no close event', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('stubborn child');
    const idle = session.waitForIdle();
    let settled = false;
    void turn.then(() => { settled = true; });

    session.interrupt();
    expect(killSpy).toHaveBeenNthCalledWith(1, -5100, 'SIGINT');
    await vi.advanceTimersByTimeAsync(3_999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(killSpy).toHaveBeenNthCalledWith(2, -5100, 'SIGTERM');
    await vi.advanceTimersByTimeAsync(5_000);
    expect(killSpy).toHaveBeenNthCalledWith(3, -5100, 'SIGKILL');
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(turn).resolves.toMatchObject({ interrupted: true, processGone: true });
    await expect(idle).resolves.toBeUndefined();
    expect(session.isAlive()).toBe(false);
  });

  it('does not settle close until the exact child is killed or closes', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('close safely');
    let settled = false;
    void turn.then(() => { settled = true; });
    session.close();
    await vi.advanceTimersByTimeAsync(3_999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(10_001);
    await expect(turn).resolves.toMatchObject({ interrupted: true, processGone: true });
    expect(killSpy).toHaveBeenCalledWith(-5100, 'SIGKILL');
  });

  it('retains tracking and scratch after an undeliverable kill until late close', async () => {
    killSpy?.mockImplementation(() => {
      throw Object.assign(new Error('not permitted'), { code: 'EPERM' });
    });
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('permission boundary');
    const scratchDir = (vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> }).env.TMPDIR;
    session.interrupt();
    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'kill_failed',
      processGone: false,
    });
    expect(session.exitDiagnostic?.()).toContain('active_child_pid=5100');
    expect(unregisterChildPid).not.toHaveBeenCalledWith(5100);
    expect(fs.existsSync(scratchDir)).toBe(true);
    let idleResolved = false;
    const idle = session.waitForIdle().then(() => { idleResolved = true; });
    await Promise.resolve();
    expect(idleResolved).toBe(false);
    session.close();
    expect(fs.existsSync(scratchDir)).toBe(true);
    children[0].close(137, 'SIGKILL');
    expect(unregisterChildPid).toHaveBeenCalledWith(5100);
    await expect(idle).resolves.toBeUndefined();
    expect(fs.existsSync(scratchDir)).toBe(false);
  });

  it('retries scratch cleanup before reporting final idle', async () => {
    const realMkdtempSync = fs.mkdtempSync.bind(fs);
    let scratchDir = '';
    vi.spyOn(fs, 'mkdtempSync').mockImplementation((prefix) => {
      const created = realMkdtempSync(prefix);
      if (prefix.includes('resident-state')) scratchDir = created;
      return created;
    });
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const realRmSync = fs.rmSync.bind(fs);
    let failedScratchCleanup = false;
    vi.spyOn(fs, 'rmSync').mockImplementation((target, options) => {
      if (target === scratchDir && !failedScratchCleanup) {
        failedScratchCleanup = true;
        throw new Error('transient cleanup failure');
      }
      realRmSync(target, options);
    });

    session.close();
    expect(fs.existsSync(scratchDir)).toBe(true);
    await expect(session.waitForIdle()).resolves.toBeUndefined();
    expect(fs.existsSync(scratchDir)).toBe(false);
  });

  it('settles child idle even when asynchronous scratch cleanup needs a retry', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('close with transient cleanup failure');
    const scratchDir = (vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> }).env.TMPDIR;
    const realRmSync = fs.rmSync.bind(fs);
    let failedScratchCleanup = false;
    vi.spyOn(fs, 'rmSync').mockImplementation((target, options) => {
      if (target === scratchDir && !failedScratchCleanup) {
        failedScratchCleanup = true;
        throw new Error('transient cleanup failure');
      }
      realRmSync(target, options);
    });

    session.close();
    const idle = session.waitForIdle();
    children[0].close(130, 'SIGINT');
    await expect(turn).resolves.toMatchObject({ interrupted: true, processGone: true });
    await expect(idle).resolves.toBeUndefined();
    expect(fs.existsSync(scratchDir)).toBe(true);
    await expect(session.waitForIdle()).resolves.toBeUndefined();
    expect(fs.existsSync(scratchDir)).toBe(false);
  });

  it('turns asynchronous stdin failure into one bounded terminal result', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    const turn = session.sendTurn('stdin breaks');
    children[0].stdin.emit('error', new Error('EPIPE'));
    await vi.advanceTimersByTimeAsync(14_000);
    await expect(turn).resolves.toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'stdin_write_failed',
      processGone: true,
    });
  });

  it('rolls back partial image materialization and synchronous spawn failure', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir, ...directIsolation() }, runner, base);
    await expect(session.sendTurn([
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'aGVsbG8=' } },
      { type: 'unsupported' },
    ])).rejects.toThrow(/unsupported content block/);
    expect(fs.readdirSync(tempDir).filter((name) => name.startsWith('.resident-codex-'))).toEqual([]);
    expect(session.isAlive()).toBe(true);

    vi.mocked(spawn).mockImplementationOnce(() => { throw new Error('spawn exploded'); });
    const result = await session.sendTurn('spawn fails');
    const spawnOptions = vi.mocked(spawn).mock.calls.at(-1)?.[2] as { env: Record<string, string> };
    expect(result).toMatchObject({
      providerFailed: true,
      providerTerminalReason: 'spawn_error',
      processGone: true,
    });
    expect(fs.existsSync(spawnOptions.env.TMPDIR)).toBe(false);
    await expect(session.waitForIdle()).resolves.toBeUndefined();
  });
});
