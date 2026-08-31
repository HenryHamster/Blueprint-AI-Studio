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
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const child = new FakeChild(5100 + children.length);
      children.push(child);
      return child as never;
    });
    killSpy = installKillSpy();
    vi.useFakeTimers();
  });

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
      workspace: tempDir,
      appendSystemPrompt: 'canonical Resident identity',
    }, runner, base);

    const first = session.sendTurn('first prompt');
    expect(children[0].stdin.end).toHaveBeenCalledWith('first prompt');
    const firstArgs = vi.mocked(spawn).mock.calls[0][1] as string[];
    expect(firstArgs.slice(0, 2)).toEqual(['exec', '--skip-git-repo-check']);
    expect(firstArgs).toContain('--ignore-user-config');
    expect(firstArgs).toContain('--strict-config');
    expect(firstArgs).toContain('default_permissions="resident-read-only"');
    expect(firstArgs).toContain('approval_policy="never"');
    expect(firstArgs).toContain('permissions.resident-read-only.extends=":read-only"');
    expect(firstArgs).toContain('permissions.resident-read-only.network.domains={"blueprint-api"="allow"}');
    expect(firstArgs).toContain('features.network_proxy=true');
    expect(firstArgs).toContain('web_search="disabled"');
    expect(firstArgs).toContain('tools.web_search=false');
    expect(firstArgs).toContain('shell_environment_policy.inherit="none"');
    const firstSpawnOptions = vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> };
    const scratchDir = firstSpawnOptions.env.TMPDIR;
    expect(scratchDir).toMatch(/blueprint-resident-codex-/);
    expect(fs.statSync(scratchDir).mode & 0o777).toBe(0o700);
    expect(firstArgs).toContain(
      `permissions.resident-read-only.filesystem={${JSON.stringify(scratchDir)}="write","/codex-home"="deny"}`,
    );
    expect(firstArgs).toContain(`shell_environment_policy.set.TMPDIR=${JSON.stringify(scratchDir)}`);
    expect(firstArgs).toContain('shell_environment_policy.set.AGENT_TOKEN_FILE="/run/blueprint/agent-token.jwt"');
    expect(firstSpawnOptions.env.AGENT_API_TOKEN).toBeUndefined();
    expect(firstSpawnOptions.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.DEEPSEEK_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.OPENAI_API_KEY).toBeUndefined();
    expect(firstSpawnOptions.env.CODEX_API_KEY).toBeUndefined();
    expect(firstArgs).not.toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(firstArgs).not.toContain('-s');
    expect(firstArgs).not.toContain('danger-full-access');
    expect(firstArgs.some((arg) => (
      arg.startsWith('developer_instructions=')
      && arg.includes('canonical Resident identity')
      && arg.includes('write only below $TMPDIR')
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
    expect(secondArgs.slice(0, 2)).toEqual(['exec', 'resume']);
    expect(secondArgs.slice(-2)).toEqual(['thread-exact-1', '-']);
    expect(secondArgs).not.toContain('--last');
    expect(secondArgs).not.toContain('--ephemeral');
    expect(secondArgs).toContain('default_permissions="resident-read-only"');
    expect(secondArgs).not.toContain('--dangerously-bypass-approvals-and-sandbox');
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
        workday: { command: process.execPath, args: ['/workspace/reply.mjs'] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);

    const first = session.sendTurn('establish exact thread');
    const firstChild = requiredLatestChild(children);
    firstChild.line({ type: 'thread.started', thread_id: 'thread-resume-proof' });
    firstChild.line({ type: 'turn.completed' });
    firstChild.close();
    await expect(first).resolves.toMatchObject({ processGone: false });

    const resumed = session.sendTurn('reply on exact resumed thread');
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
        workday: { command: process.execPath, args: ['/workspace/reply.mjs'] },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('reply before terminal failure');
    children[0].line({ type: 'thread.started', thread_id: 'thread-terminal-failure' });
    children[0].line({ type: 'item.started', item: {
      id: 'cmd-terminal', type: 'command_execution',
      command: './strategic set-memory --json exact', status: 'in_progress',
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'cmd-terminal', type: 'command_execution', status: 'completed', exit_code: 0,
      aggregated_output: '{"status":"ok","applied":true}',
    } });
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
      appliedStrategicOp: true,
      sentOperatorMessage: false,
      operatorReply: null,
    });
    expect(session.isAlive()).toBe(false);
  });

  it('requires exact successful command and MCP receipts before acknowledging authority', async () => {
    const mcpConfig = path.join(tempDir, 'resident-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: {
        workday: {
          command: process.execPath,
          args: ['/workspace/reply.mjs'],
          env: {
            RESIDENT_REPLY_RECEIPT_PATH: '/workspace/receipt.json',
            RESIDENT_STAGED_REPLY_PATH: '/workspace/staged.json',
          },
        },
      },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('mutate and reply');
    const args = vi.mocked(spawn).mock.calls[0][1] as string[];
    expect(args).toContain('mcp_servers.workday.required=true');
    expect(args).toContain('mcp_servers={}');
    expect(args).toContain(`mcp_servers.workday.command=${JSON.stringify(process.execPath)}`);
    expect(args).toContain('mcp_servers.workday.args=["/workspace/reply.mjs"]');
    expect(args).toContain(
      'mcp_servers.workday.env.RESIDENT_REPLY_RECEIPT_PATH="/workspace/receipt.json"',
    );
    expect(args).toContain(
      'mcp_servers.workday.env.RESIDENT_STAGED_REPLY_PATH="/workspace/staged.json"',
    );
    expect(args).toContain('mcp_servers.workday.enabled=true');
    expect(args).toContain('mcp_servers.workday.enabled_tools=["reply_to_operator"]');
    expect(args).toContain('mcp_servers.workday.default_tools_approval_mode="approve"');
    expect(args).toContain('mcp_servers.workday.enabled_tools=["reply_to_operator"]');

    children[0].line({ type: 'thread.started', thread_id: 'thread-tools' });
    children[0].line({ type: 'item.started', item: {
      id: 'cmd-1', type: 'command_execution',
      command: './strategic set-memory --json exact', status: 'in_progress',
    } });
    children[0].line({ type: 'item.completed', item: {
      id: 'cmd-1', type: 'command_execution', status: 'completed', exit_code: 0,
      aggregated_output: '{"status":"ok","applied":true}',
    } });
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
      appliedStrategicOp: true,
      sentOperatorMessage: true,
      operatorReply: { type: 'NOTE', content: 'operator answer' },
      toolCallCount: 2,
    });
    session.close();
  });

  it('does not accept an unarmed Workday tool result as delivery', async () => {
    const mcpConfig = path.join(tempDir, 'resident-mcp.json');
    fs.writeFileSync(mcpConfig, JSON.stringify({
      mcpServers: { workday: { command: process.execPath, args: ['/workspace/reply.mjs'] } },
    }));
    const session = startResidentCodexSession({
      ...readOnly,
      workspace: tempDir,
      replyTool: { mcpConfigPath: mcpConfig },
    }, runner, base);
    const turn = session.sendTurn('reply');
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

  it('fails closed when the process exits without turn.completed', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    expect(() => startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, unsafe))
      .toThrow(/sandbox mode workspace-write/);
    expect(() => startResidentCodexSession({ workspace: tempDir }, runner, base))
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
      workspace: tempDir,
      replyTool: { mcpConfigPath: unsafeMcp },
    }, runner, base)).toThrow(/env key RESIDENT_API_TOKEN is not allowed/);
    expect(spawn).not.toHaveBeenCalled();
  });

  it('passes only the selected API-key auth mode to the Codex parent', async () => {
    const apiRunner = { ...runner, authMode: 'api_key' } as ProviderRunner;
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, apiRunner, base);
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

  it('bounds an interrupt through SIGKILL when the child emits no close event', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    expect(fs.existsSync(scratchDir)).toBe(false);
    await expect(idle).resolves.toBeUndefined();
  });

  it('retries scratch cleanup before reporting final idle', async () => {
    const realMkdtempSync = fs.mkdtempSync.bind(fs);
    let scratchDir = '';
    vi.spyOn(fs, 'mkdtempSync').mockImplementation((prefix) => {
      scratchDir = realMkdtempSync(prefix);
      return scratchDir;
    });
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
    const realRmSync = fs.rmSync.bind(fs);
    vi.spyOn(fs, 'rmSync')
      .mockImplementationOnce(() => { throw new Error('transient cleanup failure'); })
      .mockImplementation(realRmSync);

    session.close();
    expect(fs.existsSync(scratchDir)).toBe(true);
    await expect(session.waitForIdle()).resolves.toBeUndefined();
    expect(fs.existsSync(scratchDir)).toBe(false);
  });

  it('settles child idle even when asynchronous scratch cleanup needs a retry', async () => {
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
    const turn = session.sendTurn('close with transient cleanup failure');
    const scratchDir = (vi.mocked(spawn).mock.calls[0][2] as { env: Record<string, string> }).env.TMPDIR;
    const realRmSync = fs.rmSync.bind(fs);
    vi.spyOn(fs, 'rmSync')
      .mockImplementationOnce(() => { throw new Error('transient cleanup failure'); })
      .mockImplementation(realRmSync);

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
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
    const session = startResidentCodexSession({ ...readOnly, workspace: tempDir }, runner, base);
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
