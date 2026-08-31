import { spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import {
  WORKSPACE, recordLlmCallStart, recordLlmTokens,
  recordNumTurns, recordProviderReportedUsage, sanitize,
} from '../index.js';
import {
  DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS,
  PROCESS_KILL_GRACE_MS,
} from '../constants.js';
import { isStrategicMutation } from './resident-stream.js';
import { RESIDENT_REPLY_TOOL_NAME } from './resident-reply-tool.js';
import { matchesAuthFailure, matchesRateLimit } from './runners.js';
import type { ProviderRunner, RunnerCommand } from './runners.js';
import type {
  ResidentOperatorReply, ResidentSession, ResidentSessionEvent,
  ResidentSessionOptions, ResidentTurnResult, ResidentTurnTraceEvent,
} from './resident-session.js';

const DEFAULT_TURN_STALL_MS = 120_000;
const DEFAULT_IDLE_CEILING_MS = 30 * 60_000;
const INTERRUPT_GRACE_MS = 4_000;
const FORCE_SETTLE_MS = 20_000;
const OPERATOR_REPLY_TYPES = new Set(['NOTE', 'QUESTION', 'FEEDBACK', 'BUG_REPORT', 'BLOCKER']);
const REPLY_ACCEPTED_TEXT = 'Workday reply accepted for delivery.';
const REQUIRED_READ_ONLY_TOOLS = new Set(['Write', 'Edit', 'NotebookEdit']);
const SAFE_MCP_ENV_KEYS = new Set([
  'RESIDENT_REPLY_RECEIPT_PATH',
  'RESIDENT_STAGED_REPLY_PATH',
  'CALL_RESIDENT_ROLE',
  'CALL_LOCAL_STAGE_DIR',
  'CALL_TURN_RECEIPT_PATH',
]);
const CODEX_PROCESS_ENV_KEYS = new Set([
  'HOME', 'PATH', 'LANG', 'TERM',
  'ORCHESTRATOR_URL', 'TASK_ID', 'AGENT_TOKEN_FILE',
  'BLUEPRINT_RESIDENT_INCARNATION_ID',
  'BLUEPRINT_RESIDENT_REPLY_KEY_PATH',
  'BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH',
  'BLUEPRINT_RESIDENT_STAGED_REPLY_PATH',
  'BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH',
]);
const RESIDENT_SHELL_ENV_KEYS = new Set([
  'PATH', 'LANG', 'TERM', 'ORCHESTRATOR_URL', 'TASK_ID', 'AGENT_TOKEN_FILE',
  'BLUEPRINT_RESIDENT_INCARNATION_ID',
  'BLUEPRINT_RESIDENT_REPLY_KEY_PATH',
  'BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH',
  'BLUEPRINT_RESIDENT_STAGED_REPLY_PATH',
  'BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH',
]);
const CODEX_RESIDENT_BOUNDARY = [
  'Codex Resident commands run inside an enforced read-only, no-approval sandbox.',
  'Do not attempt filesystem writes or approval escalation.',
  'For required temporary files, write only below $TMPDIR; never write to /tmp directly.',
  'Repository synchronization is coordinator-owned; do not run git fetch or change refs.',
].join(' ');

interface CodexTurn {
  resolve: (result: ResidentTurnResult) => void;
  childGeneration: number;
  startedAtMs: number;
  lastActivityAtMs: number;
  firstTokenAtMs: number | null;
  resultText: string;
  accumulated: string;
  interrupted: boolean;
  timedOut: boolean;
  hardTimedOut: boolean;
  providerFailed: boolean;
  failureCategory: 'rate_limited' | 'dead_auth' | null;
  failureDetail: string | null;
  providerTerminalReason: string | null;
  providerOutputTail: string | null;
  sawToolUse: boolean;
  appliedStrategicOp: boolean;
  sentOperatorMessage: boolean;
  operatorReply: ResidentOperatorReply | null;
  inputTokens: number;
  toolCallCount: number;
  totalToolCallDurationMs: number;
  toolStarts: Map<string, number>;
  seenTools: Set<string>;
  pendingReplies: Map<string, ResidentOperatorReply>;
  pendingStrategic: Set<string>;
  trace: ResidentTurnTraceEvent[];
  traceTruncated: boolean;
  threadReceiptValid: boolean;
  terminalCompleted: boolean;
  terminalFailed: boolean;
  stallTimeoutMs: number;
  hardTimeoutMs: number;
  stallTimer: NodeJS.Timeout | null;
  hardTimer: NodeJS.Timeout | null;
  forceTimer: NodeJS.Timeout | null;
  killTimer: NodeJS.Timeout | null;
  settleTimer: NodeJS.Timeout | null;
  terminationStarted: boolean;
}

function tomlString(value: string): string {
  return JSON.stringify(value);
}

function tomlStringArray(values: readonly string[]): string {
  return `[${values.map(tomlString).join(',')}]`;
}

function tomlStringMap(entries: ReadonlyArray<readonly [string, string]>): string {
  return `{${entries.map(([key, value]) => `${tomlString(key)}=${tomlString(value)}`).join(',')}}`;
}

function safeConfigKey(value: string, label: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error(`invalid ${label} in Resident MCP config`);
  return value;
}

function codexMcpArgs(opts: ResidentSessionOptions): string[] {
  if (!opts.replyTool) return ['-c', 'mcp_servers={}'];
  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(opts.replyTool.mcpConfigPath, 'utf8'));
  } catch (error) {
    throw new Error(`Resident MCP config is unreadable: ${sanitize(String(error))}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Resident MCP config must be an object');
  }
  const rawServers = (parsed as Record<string, unknown>).mcpServers;
  if (!rawServers || typeof rawServers !== 'object' || Array.isArray(rawServers)) {
    throw new Error('Resident MCP config must contain mcpServers');
  }
  const allowedTools = opts.replyTool.allowedTools ?? [opts.replyTool.toolName ?? RESIDENT_REPLY_TOOL_NAME];
  const allowedByServer = new Map<string, string[]>();
  for (const fullName of allowedTools) {
    const match = /^mcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_-]+)$/.exec(fullName);
    if (!match) throw new Error(`invalid Resident MCP tool name: ${fullName}`);
    const tools = allowedByServer.get(match[1]) ?? [];
    tools.push(match[2]);
    allowedByServer.set(match[1], tools);
  }
  const args = ['-c', 'mcp_servers={}'];
  for (const [serverName, enabledTools] of allowedByServer) {
    const server = (rawServers as Record<string, unknown>)[serverName];
    if (!server || typeof server !== 'object' || Array.isArray(server)) {
      throw new Error(`Resident MCP config is missing server ${serverName}`);
    }
    const config = server as Record<string, unknown>;
    if (typeof config.command !== 'string' || !config.command) {
      throw new Error(`Resident MCP server ${serverName} has no command`);
    }
    const serverKey = safeConfigKey(serverName, 'server name');
    args.push('-c', `mcp_servers.${serverKey}.command=${tomlString(config.command)}`);
    const serverArgs = config.args;
    if (serverArgs !== undefined) {
      if (!Array.isArray(serverArgs) || serverArgs.some((item) => typeof item !== 'string')) {
        throw new Error(`Resident MCP server ${serverName} args must be strings`);
      }
      args.push('-c', `mcp_servers.${serverKey}.args=${tomlStringArray(serverArgs as string[])}`);
    }
    const serverEnv = config.env;
    if (serverEnv !== undefined) {
      if (!serverEnv || typeof serverEnv !== 'object' || Array.isArray(serverEnv)) {
        throw new Error(`Resident MCP server ${serverName} env must be an object`);
      }
      for (const [envName, envValue] of Object.entries(serverEnv)) {
        if (typeof envValue !== 'string') throw new Error(`Resident MCP server ${serverName} env must be strings`);
        if (!SAFE_MCP_ENV_KEYS.has(envName)) {
          throw new Error(`Resident MCP server ${serverName} env key ${envName} is not allowed`);
        }
        args.push(
          '-c',
          `mcp_servers.${serverKey}.env.${safeConfigKey(envName, 'environment key')}=${tomlString(envValue)}`,
        );
      }
    }
    args.push(
      '-c', `mcp_servers.${serverKey}.enabled=true`,
      '-c', `mcp_servers.${serverKey}.required=true`,
      '-c', `mcp_servers.${serverKey}.enabled_tools=${tomlStringArray(enabledTools)}`,
      '-c', `mcp_servers.${serverKey}.default_tools_approval_mode=${tomlString('approve')}`,
    );
  }
  return args;
}

function normalizeCommonArgs(
  base: RunnerCommand,
  opts: ResidentSessionOptions,
  scratchDir: string,
  childEnv: NodeJS.ProcessEnv,
): string[] {
  if (base.args[0] !== 'exec') throw new Error('Codex Resident runner must start with codex exec');
  const valueFlags = new Set(['-c', '--config', '-m', '--model', '--enable', '--disable', '--thread-source']);
  const flagOnly = new Set([
    '--json', '--skip-git-repo-check', '--ignore-user-config', '--strict-config',
  ]);
  const common: string[] = [];
  for (let index = 1; index < base.args.length; index += 1) {
    const arg = base.args[index];
    if (arg === '-s') {
      const value = base.args.at(index + 1);
      if (value !== 'danger-full-access') {
        throw new Error(`Codex Resident cannot safely resume runner sandbox mode ${value ?? '<missing>'}`);
      }
      index += 1;
      continue;
    }
    if (valueFlags.has(arg)) {
      const value = base.args.at(index + 1);
      if (value === undefined || value.startsWith('-')) throw new Error(`Codex Resident flag ${arg} is missing a value`);
      common.push(arg, value);
      index += 1;
    } else if (arg === '--dangerously-bypass-approvals-and-sandbox') {
      // Ordinary one-shot Codex tasks are writable. Resident is a protected
      // read-only role, so replace this only recognized broad mode below.
      continue;
    } else if (flagOnly.has(arg)) {
      common.push(arg);
    } else {
      throw new Error(`Codex Resident cannot safely resume runner argument ${arg}`);
    }
  }
  if (!common.includes('--json')) common.push('--json');
  if (!common.includes('--skip-git-repo-check')) common.push('--skip-git-repo-check');
  if (!common.includes('--ignore-user-config')) common.push('--ignore-user-config');
  if (!common.includes('--strict-config')) common.push('--strict-config');
  common.push(...codexMcpArgs(opts));
  const orchestratorUrl = base.env.ORCHESTRATOR_URL;
  let orchestratorHost = '';
  try {
    const parsed = new URL(orchestratorUrl);
    if (
      !['http:', 'https:'].includes(parsed.protocol)
      || parsed.username || parsed.password
      || !parsed.hostname
      || parsed.hostname.includes('*')
      || !/^[A-Za-z0-9.:-]+$/.test(parsed.hostname)
    ) {
      throw new Error('unsupported control-plane URL');
    }
    orchestratorHost = parsed.hostname;
  } catch {
    throw new Error('Codex Resident requires an exact HTTP(S) control-plane host');
  }
  // Append these last so provider translation cannot broaden the Resident.
  // The named profile inherits a read-only filesystem and permits command
  // network traffic only to this task's exact Blueprint control-plane host.
  const filesystemRules: Array<readonly [string, string]> = [[scratchDir, 'write']];
  const codexHome = childEnv.CODEX_HOME;
  if (codexHome) filesystemRules.push([codexHome, 'deny']);
  common.push(
    '-c', 'permissions.resident-read-only.extends=":read-only"',
    '-c', `permissions.resident-read-only.filesystem=${tomlStringMap(filesystemRules)}`,
    '-c', 'permissions.resident-read-only.network.enabled=true',
    '-c', 'permissions.resident-read-only.network.mode="limited"',
    '-c', `permissions.resident-read-only.network.domains=${tomlStringMap([[orchestratorHost, 'allow']])}`,
    '-c', 'features.network_proxy=true',
    '-c', 'default_permissions="resident-read-only"',
    '-c', 'approval_policy="never"',
    '-c', 'web_search="disabled"',
    '-c', 'tools.web_search=false',
    '-c', 'shell_environment_policy.inherit="none"',
    '-c', 'shell_environment_policy.ignore_default_excludes=false',
    '-c', `shell_environment_policy.set.HOME=${tomlString(scratchDir)}`,
  );
  for (const [envName, envValue] of Object.entries(childEnv)) {
    if (
      typeof envValue === 'string'
      && (envName === 'TMPDIR' || RESIDENT_SHELL_ENV_KEYS.has(envName))
    ) {
      common.push(
        '-c', `shell_environment_policy.set.${safeConfigKey(envName, 'shell environment key')}=${tomlString(envValue)}`,
      );
    }
  }
  const developerInstructions = [opts.appendSystemPrompt, CODEX_RESIDENT_BOUNDARY]
    .filter((value): value is string => Boolean(value))
    .join('\n\n');
  common.push('-c', `developer_instructions=${tomlString(developerInstructions)}`);
  return common;
}

function operatorReply(input: unknown): ResidentOperatorReply | null {
  let value = input;
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return null; }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const content = typeof raw.content === 'string' ? raw.content.trim() : '';
  const type = raw.message_type === undefined ? 'NOTE' : raw.message_type;
  if (!content || typeof type !== 'string' || !OPERATOR_REPLY_TYPES.has(type)) return null;
  return { content, type };
}

function strategicAcknowledged(output: unknown): boolean {
  if (typeof output !== 'string') return false;
  let parsed: unknown;
  try { parsed = JSON.parse(output.trim()); } catch { return false; }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
  const receipt = parsed as Record<string, unknown>;
  if (receipt.applied === false || receipt.status === 'refused' || receipt.http_status === 409) return false;
  return receipt.applied === true || receipt.status === 'ok';
}

function mcpReplyAccepted(result: unknown): boolean {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false;
  const raw = result as Record<string, unknown>;
  const structured = raw.structured_content ?? raw.structuredContent;
  const accepted = Boolean(
    structured && typeof structured === 'object' && !Array.isArray(structured)
    && (structured as Record<string, unknown>).accepted === true,
  );
  const content = Array.isArray(raw.content) ? raw.content : [];
  const exactText = content.some((entry) => (
    entry && typeof entry === 'object' && !Array.isArray(entry)
    && (entry as Record<string, unknown>).type === 'text'
    && (entry as Record<string, unknown>).text === REPLY_ACCEPTED_TEXT
  ));
  return accepted && exactText;
}

function boundedTail(current: string, addition: string, max = 16_000): string {
  return (current + addition).slice(-max);
}

function contentPrompt(
  input: string | Array<Record<string, unknown>>,
  workspace: string,
  generation: number,
): { prompt: string; imagePaths: string[] } {
  if (typeof input === 'string') return { prompt: input, imagePaths: [] };
  const text: string[] = [];
  const imagePaths: string[] = [];
  try {
    for (const [index, block] of input.entries()) {
      if (block.type === 'text' && typeof block.text === 'string') {
        text.push(block.text);
        continue;
      }
      if (block.type !== 'image' || !block.source || typeof block.source !== 'object') {
        throw new Error('Codex Resident received an unsupported content block');
      }
      const source = block.source as Record<string, unknown>;
      if (source.type !== 'base64' || typeof source.data !== 'string') {
        throw new Error('Codex Resident image block must be materialized as base64');
      }
      const extension = source.media_type === 'image/png' ? 'png'
        : source.media_type === 'image/jpeg' ? 'jpg'
          : source.media_type === 'image/webp' ? 'webp'
            : source.media_type === 'image/gif' ? 'gif' : null;
      if (!extension) throw new Error(`Codex Resident does not support image type ${String(source.media_type)}`);
      const imagePath = path.join(workspace, `.resident-codex-${process.pid}-${generation}-${index}.${extension}`);
      fs.writeFileSync(imagePath, Buffer.from(source.data, 'base64'), { flag: 'wx', mode: 0o600 });
      imagePaths.push(imagePath);
    }
  } catch (error) {
    for (const imagePath of imagePaths) {
      try { fs.unlinkSync(imagePath); } catch { /* best-effort rollback */ }
    }
    throw error;
  }
  return { prompt: text.join(''), imagePaths };
}

export function startResidentCodexSession(
  opts: ResidentSessionOptions,
  runner: ProviderRunner,
  runnerBase: RunnerCommand,
): ResidentSession {
  if (opts.foregroundLaunch) throw new Error('Codex Resident does not support the Claude-only foreground launch');
  if (runnerBase.outputMode !== 'codex-jsonl') throw new Error('Codex Resident requires codex-jsonl output');
  const disallowed = new Set(opts.disallowedTools ?? []);
  if ([...REQUIRED_READ_ONLY_TOOLS].some((tool) => !disallowed.has(tool))) {
    throw new Error('Codex Resident requires the complete read-only tool boundary');
  }
  const workspace = opts.workspace ?? WORKSPACE;
  const turnTimeoutMs = opts.turnTimeoutMs ?? DEFAULT_TURN_STALL_MS;
  const hardTurnTimeoutMs = opts.hardTurnTimeoutMs
    ?? Math.max(DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS, turnTimeoutMs + FORCE_SETTLE_MS);
  const idleCeilingMs = opts.idleCeilingMs ?? DEFAULT_IDLE_CEILING_MS;
  const scratchDir = fs.mkdtempSync(path.join(os.tmpdir(), 'blueprint-resident-codex-'));
  fs.chmodSync(scratchDir, 0o700);
  const mergedEnv = { ...runnerBase.env, ...opts.env };
  const childEnv: NodeJS.ProcessEnv = { TMPDIR: scratchDir };
  for (const [envName, envValue] of Object.entries(mergedEnv)) {
    if (CODEX_PROCESS_ENV_KEYS.has(envName)) childEnv[envName] = envValue;
  }
  if (runner.authMode === 'oauth') {
    if (mergedEnv.CODEX_HOME) childEnv.CODEX_HOME = mergedEnv.CODEX_HOME;
  } else {
    if (mergedEnv.OPENAI_API_KEY) childEnv.OPENAI_API_KEY = mergedEnv.OPENAI_API_KEY;
    if (mergedEnv.CODEX_API_KEY) childEnv.CODEX_API_KEY = mergedEnv.CODEX_API_KEY;
  }
  let commonArgs: string[];
  try {
    commonArgs = normalizeCommonArgs(runnerBase, opts, scratchDir, childEnv);
  } catch (error) {
    fs.rmSync(scratchDir, { recursive: true, force: true });
    throw error;
  }
  const listeners: Array<(event: ResidentSessionEvent) => void> = [];
  const idleWaiters: Array<() => void> = [];
  let sessionIdValue: string | null = null;
  let alive = true;
  let closed = false;
  let active: CodexTurn | null = null;
  let activeChild: ReturnType<typeof spawn> | null = null;
  let childGeneration = 0;
  let lastExitCode: number | null = null;
  let failureTail = '';
  let terminalFailure: 'rate_limited' | 'dead_auth' | null = null;
  let idleTimer: NodeJS.Timeout | null = null;
  const imagePaths = new Set<string>();
  let scratchSafeToRemove = true;
  let scratchRemoved = false;

  const emit = (event: ResidentSessionEvent): void => {
    for (const listener of listeners) {
      try { listener(event); } catch { /* subscriber failure cannot break transport */ }
    }
  };
  const classify = (text: string): 'rate_limited' | 'dead_auth' | null => {
    if (matchesRateLimit(text, runner.provider, runner.authMode)) return 'rate_limited';
    if (matchesAuthFailure(text, runner.provider)) return 'dead_auth';
    return null;
  };
  const cleanupImages = (): void => {
    for (const imagePath of imagePaths) {
      try { fs.unlinkSync(imagePath); } catch { /* already gone */ }
      imagePaths.delete(imagePath);
    }
  };
  const cleanupScratch = (): boolean => {
    if (scratchRemoved) return true;
    if (!scratchSafeToRemove) return false;
    try {
      fs.rmSync(scratchDir, { recursive: true, force: true });
      scratchRemoved = true;
      return true;
    } catch {
      failureTail = boundedTail(failureTail, '\nresident_scratch_cleanup_failed');
      return false;
    }
  };
  const resolveIdle = (): void => {
    while (idleWaiters.length) idleWaiters.shift()?.();
  };
  const clearTimers = (turn: CodexTurn): void => {
    if (turn.stallTimer) clearTimeout(turn.stallTimer);
    if (turn.hardTimer) clearTimeout(turn.hardTimer);
    if (turn.forceTimer) clearTimeout(turn.forceTimer);
    if (turn.killTimer) clearTimeout(turn.killTimer);
    if (turn.settleTimer) clearTimeout(turn.settleTimer);
    turn.stallTimer = turn.hardTimer = turn.forceTimer = turn.killTimer = turn.settleTimer = null;
  };
  const armIdle = (): void => {
    if (!alive || closed) return;
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { close(); }, idleCeilingMs);
  };
  const trace = (turn: CodexTurn, kind: ResidentTurnTraceEvent['kind'], label?: string, isError?: boolean): void => {
    turn.lastActivityAtMs = Date.now();
    if (turn.trace.length >= 128) {
      turn.trace.splice(32, 1);
      turn.traceTruncated = true;
    }
    turn.trace.push({
      elapsedMs: turn.lastActivityAtMs - turn.startedAtMs,
      kind,
      ...(label ? { label: label.slice(0, 80) } : {}),
      ...(isError === undefined ? {} : { isError }),
    });
  };
  const resultFor = (turn: CodexTurn, processGone: boolean): ResidentTurnResult => ({
    resultText: turn.resultText || turn.accumulated,
    accumulatedTurnText: turn.accumulated,
    interrupted: turn.interrupted,
    timedOut: turn.timedOut,
    hardTimedOut: turn.hardTimedOut,
    appliedStrategicOp: turn.appliedStrategicOp,
    sawToolUse: turn.sawToolUse,
    sentOperatorMessage: turn.sentOperatorMessage,
    operatorReply: turn.operatorReply,
    processGone,
    ttftMs: turn.firstTokenAtMs === null ? null : turn.firstTokenAtMs - turn.startedAtMs,
    turnMs: Date.now() - turn.startedAtMs,
    toolCallCount: turn.toolCallCount,
    totalToolCallDurationMs: turn.totalToolCallDurationMs,
    meanInterTokenMs: null,
    inputTokens: turn.inputTokens,
    providerFailed: turn.providerFailed,
    failureCategory: turn.failureCategory,
    failureDetail: turn.failureDetail,
    providerTerminalReason: turn.providerTerminalReason,
    providerOutputTail: turn.providerOutputTail,
    trace: turn.trace,
    traceTruncated: turn.traceTruncated,
  });
  const settle = (
    turn: CodexTurn,
    processGone: boolean,
    childConfirmedGone = processGone,
  ): void => {
    if (active !== turn) return;
    active = null;
    if (childConfirmedGone) activeChild = null;
    clearTimers(turn);
    if (childConfirmedGone) cleanupImages();
    if (childConfirmedGone && (!alive || closed)) cleanupScratch();
    armIdle();
    turn.resolve(resultFor(turn, processGone));
    if (childConfirmedGone) resolveIdle();
  };
  const markFailure = (turn: CodexTurn, reason: string, detail: string): void => {
    const clean = sanitize(detail).slice(-2_000);
    failureTail = boundedTail(failureTail, `\n${clean}`);
    turn.providerFailed = true;
    turn.providerTerminalReason = reason;
    turn.failureDetail = clean || reason;
    turn.providerOutputTail = clean || null;
    turn.failureCategory = classify(failureTail);
    terminalFailure = turn.failureCategory;
    trace(turn, 'error', reason, true);
  };
  const signalTurn = (
    turn: CodexTurn,
    signal: NodeJS.Signals,
  ): 'delivered' | 'gone' | 'failed' => {
    if (active !== turn || turn.childGeneration !== childGeneration) return 'gone';
    const pid = activeChild?.pid;
    if (typeof pid !== 'number') return 'gone';
    let groupError: unknown = null;
    try {
      process.kill(-pid, signal);
      return 'delivered';
    } catch (error) {
      groupError = error;
      try {
        process.kill(pid, signal);
        return 'delivered';
      } catch (leaderError) {
        const codes = [groupError, leaderError].map((error) => (
          error && typeof error === 'object' && 'code' in error
            ? String((error as { code?: unknown }).code) : ''
        ));
        return codes.some((code) => code === 'EPERM' || code === 'EACCES')
          ? 'failed' : 'gone';
      }
    }
  };
  const beginTermination = (turn: CodexTurn, initial: 'SIGINT' | 'SIGTERM'): void => {
    if (active !== turn || turn.terminationStarted) return;
    turn.terminationStarted = true;
    const initialResult = signalTurn(turn, initial);
    if (initialResult !== 'delivered') {
      if (initialResult === 'failed') {
        scratchSafeToRemove = false;
        markFailure(turn, 'kill_failed', `Could not deliver ${initial} to Codex child`);
      }
      settle(turn, initialResult === 'gone', initialResult === 'gone');
      return;
    }
    const escalateToKill = (): void => {
      if (active !== turn) return;
      const killResult = signalTurn(turn, 'SIGKILL');
      if (killResult !== 'delivered') {
        if (killResult === 'failed') {
          scratchSafeToRemove = false;
          markFailure(turn, 'kill_failed', 'Could not deliver SIGKILL to Codex child');
        }
        settle(turn, killResult === 'gone', killResult === 'gone');
        return;
      }
      turn.settleTimer = setTimeout(() => {
        if (active !== turn) return;
        const pid = activeChild?.pid;
        let processGone = typeof pid !== 'number';
        if (typeof pid === 'number') {
          try {
            process.kill(pid, 0);
          } catch (error) {
            const code = error && typeof error === 'object' && 'code' in error
              ? String((error as { code?: unknown }).code) : '';
            processGone = code === 'ESRCH';
          }
          if (processGone) unregisterChildPid(pid);
        }
        if (!processGone) {
          scratchSafeToRemove = false;
          markFailure(turn, 'kill_failed', 'Codex child remained live after SIGKILL grace');
        }
        settle(turn, processGone, processGone);
      }, PROCESS_KILL_GRACE_MS);
    };
    if (initial === 'SIGINT') {
      turn.forceTimer = setTimeout(() => {
        if (active !== turn) return;
        const termResult = signalTurn(turn, 'SIGTERM');
        if (termResult !== 'delivered') {
          if (termResult === 'failed') {
            scratchSafeToRemove = false;
            markFailure(turn, 'kill_failed', 'Could not deliver SIGTERM to Codex child');
          }
          settle(turn, termResult === 'gone', termResult === 'gone');
          return;
        }
        turn.killTimer = setTimeout(escalateToKill, PROCESS_KILL_GRACE_MS);
      }, INTERRUPT_GRACE_MS);
    } else {
      turn.killTimer = setTimeout(escalateToKill, PROCESS_KILL_GRACE_MS);
    }
  };
  const armStall = (turn: CodexTurn): void => {
    if (turn.stallTimer) clearTimeout(turn.stallTimer);
    turn.stallTimer = setTimeout(() => {
      if (active !== turn) return;
      turn.interrupted = true;
      turn.timedOut = true;
      alive = false;
      beginTermination(turn, 'SIGINT');
    }, turn.stallTimeoutMs);
  };
  const armHard = (turn: CodexTurn): void => {
    turn.hardTimer = setTimeout(() => {
      if (active !== turn) return;
      turn.interrupted = true;
      turn.timedOut = true;
      turn.hardTimedOut = true;
      alive = false;
      beginTermination(turn, 'SIGINT');
    }, turn.hardTimeoutMs);
  };

  const handleItem = (turn: CodexTurn, eventType: string, item: Record<string, unknown>): void => {
    const itemType = typeof item.type === 'string' ? item.type : '';
    const id = typeof item.id === 'string' ? item.id : `${itemType}:anonymous`;
    const completed = eventType === 'item.completed';
    const started = eventType === 'item.started';
    if (itemType === 'reasoning') {
      trace(turn, 'text', 'reasoning');
      return;
    }
    if (itemType === 'agent_message') {
      if (!completed || typeof item.text !== 'string') return;
      turn.resultText = item.text;
      turn.accumulated += item.text;
      if (turn.firstTokenAtMs === null) turn.firstTokenAtMs = Date.now();
      trace(turn, 'text', 'agent_message');
      emit({ kind: 'text', text: item.text });
      return;
    }
    const mcpTool = typeof item.tool === 'string' ? item.tool : '';
    const toolName = itemType === 'command_execution' ? 'Bash'
      : itemType === 'file_change' ? 'apply_patch'
        : itemType === 'mcp_tool_call' ? `MCP:${mcpTool}`
          : itemType === 'web_search' ? 'WebSearch'
            : itemType === 'todo_list' ? 'TodoWrite' : null;
    if (!toolName) {
      if (itemType === 'error') trace(turn, 'error', 'item_error', true);
      return;
    }
    const firstObservation = !turn.seenTools.has(id);
    if ((started || (completed && firstObservation)) && firstObservation) {
      turn.seenTools.add(id);
      turn.sawToolUse = true;
      turn.toolCallCount += 1;
      turn.toolStarts.set(id, Date.now());
      trace(turn, 'tool', toolName);
      const activityKind = itemType === 'command_execution' ? 'command'
        : itemType === 'file_change' ? 'file' : 'tool';
      emit({ kind: 'tool', toolName, activity: { kind: activityKind, text: toolName } });
      if (itemType === 'command_execution' && typeof item.command === 'string' && isStrategicMutation(item.command)) {
        turn.pendingStrategic.add(id);
      }
      if (itemType === 'mcp_tool_call') {
        const server = item.server;
        const tool = item.tool;
        if (server === 'workday' && tool === 'reply_to_operator') {
          const reply = operatorReply(item.arguments);
          if (reply) turn.pendingReplies.set(id, reply);
        }
      }
    }
    if (!completed) return;
    const began = turn.toolStarts.get(id);
    if (began !== undefined) turn.totalToolCallDurationMs += Date.now() - began;
    const status = typeof item.status === 'string' ? item.status : '';
    const exitCode = typeof item.exit_code === 'number' ? item.exit_code : null;
    const succeeded = status === 'completed' && (itemType !== 'command_execution' || exitCode === 0);
    trace(turn, 'tool_result', toolName, !succeeded);
    emit({ kind: 'activity', activity: { kind: 'tool_result', text: `${toolName}: ${status || 'completed'}` } });
    if (turn.pendingStrategic.delete(id) && succeeded && strategicAcknowledged(item.aggregated_output)) {
      turn.appliedStrategicOp = true;
    }
    const reply = turn.pendingReplies.get(id);
    turn.pendingReplies.delete(id);
    if (
      reply && succeeded && item.error == null && mcpReplyAccepted(item.result)
    ) {
      turn.sentOperatorMessage = true;
      turn.operatorReply = reply;
    }
  };

  const handleLine = (turn: CodexTurn, line: string): void => {
    let raw: unknown;
    try { raw = JSON.parse(line); } catch {
      failureTail = boundedTail(failureTail, `\n${sanitize(line)}`);
      return;
    }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return;
    const event = raw as Record<string, unknown>;
    const eventType = typeof event.type === 'string' ? event.type : '';
    turn.lastActivityAtMs = Date.now();
    armStall(turn);
    if (eventType === 'thread.started') {
      const incoming = typeof event.thread_id === 'string' ? event.thread_id : '';
      if (!incoming) {
        markFailure(turn, 'missing_thread_id', 'Codex thread.started omitted thread_id');
      } else if (sessionIdValue !== null && sessionIdValue !== incoming) {
        markFailure(turn, 'thread_id_mismatch', `Codex resume changed thread id from ${sessionIdValue} to ${incoming}`);
      } else {
        sessionIdValue = incoming;
        turn.threadReceiptValid = true;
      }
      return;
    }
    if (eventType === 'turn.started') {
      emit({ kind: 'status', text: 'Codex thinking' });
      return;
    }
    if (eventType === 'item.started' || eventType === 'item.updated' || eventType === 'item.completed') {
      if (event.item && typeof event.item === 'object' && !Array.isArray(event.item)) {
        handleItem(turn, eventType, event.item as Record<string, unknown>);
      }
      return;
    }
    if (eventType === 'turn.completed') {
      turn.terminalCompleted = true;
      if (!turn.providerFailed) turn.providerTerminalReason = 'completed';
      trace(turn, 'result', 'completed', false);
      const usage = event.usage;
      if (usage && typeof usage === 'object' && !Array.isArray(usage)) {
        const u = usage as Record<string, unknown>;
        const input = Number(u.input_tokens ?? 0) || 0;
        const cached = Number(u.cached_input_tokens ?? 0) || 0;
        const output = Number(u.output_tokens ?? 0) || 0;
        turn.inputTokens = input;
        recordProviderReportedUsage(Math.max(input - cached, 0), output, 0, cached);
        recordLlmTokens(input, output, 0, cached);
        recordNumTurns(1);
      }
      return;
    }
    if (eventType === 'turn.failed') {
      turn.terminalFailed = true;
      const envelope = event.error;
      const envelopeMessage = envelope && typeof envelope === 'object' && !Array.isArray(envelope)
        ? (envelope as Record<string, unknown>).message
        : null;
      const message = typeof envelopeMessage === 'string' && envelopeMessage
        ? envelopeMessage
        : 'Codex turn failed';
      markFailure(turn, 'turn_failed', message);
      return;
    }
    if (eventType === 'error') {
      const message = typeof event.message === 'string' && event.message
        ? event.message
        : 'Codex provider error';
      markFailure(turn, 'provider_error', message);
      emit({ kind: 'error', text: message });
    }
  };

  const startChild = (turn: CodexTurn, prompt: string, turnImages: string[]): void => {
    const resume = sessionIdValue !== null;
    const args = resume
      ? ['exec', 'resume', ...commonArgs, ...turnImages.flatMap((image) => ['-i', image]), sessionIdValue as string, '-']
      : ['exec', ...commonArgs, ...turnImages.flatMap((image) => ['-i', image]), '-'];
    const child = spawn(runnerBase.command, args, {
      cwd: workspace,
      env: childEnv,
      detached: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    activeChild = child;
    const generation = turn.childGeneration;
    const pid = child.pid;
    if (typeof pid === 'number') registerChildPid(pid);
    recordLlmCallStart();
    let carry = '';
    let stderr = '';
    child.stdout.on('data', (data: Buffer) => {
      if (active !== turn || turn.childGeneration !== generation) return;
      turn.lastActivityAtMs = Date.now();
      armStall(turn);
      const combined = carry + data.toString();
      const parts = combined.split('\n');
      carry = parts.pop() ?? '';
      for (const line of parts) if (line.trim()) handleLine(turn, line);
    });
    child.stderr.on('data', (data: Buffer) => {
      if (active !== turn || turn.childGeneration !== generation) return;
      stderr = boundedTail(stderr, sanitize(data.toString()));
      failureTail = boundedTail(failureTail, sanitize(data.toString()));
      turn.lastActivityAtMs = Date.now();
      armStall(turn);
    });
    child.on('error', (error: Error) => {
      if (active !== turn || turn.childGeneration !== generation) return;
      markFailure(turn, 'spawn_error', error.message);
      alive = false;
      beginTermination(turn, 'SIGINT');
    });
    child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
      if (typeof pid === 'number') unregisterChildPid(pid);
      if (active !== turn || turn.childGeneration !== generation) {
        if (activeChild?.pid === pid) activeChild = null;
        scratchSafeToRemove = true;
        cleanupImages();
        if (!alive || closed) cleanupScratch();
        if (!active && !activeChild) resolveIdle();
        return;
      }
      if (carry.trim()) handleLine(turn, carry);
      lastExitCode = code;
      const cleanExit = code === 0 && signal === null;
      const validThread = turn.threadReceiptValid && sessionIdValue !== null;
      const success = cleanExit && turn.terminalCompleted && !turn.terminalFailed && !turn.providerFailed && validThread;
      if (!success && !turn.providerFailed && !turn.interrupted) {
        const reason = !validThread ? 'missing_thread_id'
          : !turn.terminalCompleted ? 'missing_turn_completed'
            : !cleanExit ? 'process_exit' : 'terminal_failure';
        markFailure(turn, reason, stderr || `Codex exited code=${String(code)} signal=${String(signal)}`);
      }
      if (turn.interrupted || !success) {
        // A reply receipt from a process whose thread identity or terminal
        // predicate failed is durable ambiguity evidence, not authorization to
        // publish. Keep strategic-op evidence for reconciliation, but revoke
        // the local reply projection before the handler can consume it.
        turn.sentOperatorMessage = false;
        turn.operatorReply = null;
        alive = false;
        if (opts.onExit) { try { opts.onExit(code); } catch { /* ignore */ } }
      } else {
        failureTail = '';
        terminalFailure = null;
      }
      settle(turn, turn.interrupted || !success, true);
    });
    child.stdin.on('error', (error: Error) => {
      if (active !== turn || turn.childGeneration !== generation) return;
      markFailure(turn, 'stdin_write_failed', error.message);
      alive = false;
      beginTermination(turn, 'SIGINT');
    });
    try {
      child.stdin.end(prompt);
    } catch (error) {
      markFailure(turn, 'stdin_write_failed', String(error));
      alive = false;
      beginTermination(turn, 'SIGINT');
    }
  };

  function sendTurn(
    input: string | Array<Record<string, unknown>>,
    turnOpts?: { stallTimeoutMs?: number; hardTimeoutMs?: number },
  ): Promise<ResidentTurnResult> {
    if (!alive || closed) {
      const now = Date.now();
      const inert: CodexTurn = {
        resolve: () => undefined, childGeneration, startedAtMs: now, lastActivityAtMs: now,
        firstTokenAtMs: null, resultText: '', accumulated: '', interrupted: false,
        timedOut: false, hardTimedOut: false, providerFailed: false,
        failureCategory: terminalFailure, failureDetail: null, providerTerminalReason: null,
        providerOutputTail: null, sawToolUse: false, appliedStrategicOp: false,
        sentOperatorMessage: false, operatorReply: null, inputTokens: 0, toolCallCount: 0,
        totalToolCallDurationMs: 0, toolStarts: new Map(), seenTools: new Set(),
        pendingReplies: new Map(), pendingStrategic: new Set(), trace: [], traceTruncated: false,
        threadReceiptValid: false, terminalCompleted: false, terminalFailed: false,
        stallTimeoutMs: turnTimeoutMs,
        hardTimeoutMs: hardTurnTimeoutMs, stallTimer: null, hardTimer: null, forceTimer: null,
        killTimer: null, settleTimer: null, terminationStarted: false,
      };
      return Promise.resolve(resultFor(inert, true));
    }
    if (active) return Promise.reject(new Error('resident sendTurn called while a turn is already active'));
    let materialized: { prompt: string; imagePaths: string[] };
    try {
      materialized = contentPrompt(input, scratchDir, childGeneration + 1);
      for (const image of materialized.imagePaths) imagePaths.add(image);
    } catch (error) {
      return Promise.reject(
        error instanceof Error
          ? error
          : new Error(typeof error === 'string' ? error : 'Resident turn materialization failed'),
      );
    }
    const startedAtMs = Date.now();
    const stallTimeout = typeof turnOpts?.stallTimeoutMs === 'number' && Number.isFinite(turnOpts.stallTimeoutMs)
      ? Math.max(turnTimeoutMs, turnOpts.stallTimeoutMs) : turnTimeoutMs;
    const hardTimeout = typeof turnOpts?.hardTimeoutMs === 'number' && Number.isFinite(turnOpts.hardTimeoutMs)
      ? Math.max(1, Math.round(turnOpts.hardTimeoutMs)) : hardTurnTimeoutMs;
    return new Promise<ResidentTurnResult>((resolve) => {
      const turn: CodexTurn = {
        resolve, childGeneration: ++childGeneration, startedAtMs, lastActivityAtMs: startedAtMs,
        firstTokenAtMs: null, resultText: '', accumulated: '', interrupted: false,
        timedOut: false, hardTimedOut: false, providerFailed: false, failureCategory: null,
        failureDetail: null, providerTerminalReason: null, providerOutputTail: null,
        sawToolUse: false, appliedStrategicOp: false, sentOperatorMessage: false,
        operatorReply: null, inputTokens: 0, toolCallCount: 0, totalToolCallDurationMs: 0,
        toolStarts: new Map(), seenTools: new Set(), pendingReplies: new Map(),
        pendingStrategic: new Set(), trace: [], traceTruncated: false,
        threadReceiptValid: false, terminalCompleted: false, terminalFailed: false,
        stallTimeoutMs: stallTimeout,
        hardTimeoutMs: hardTimeout, stallTimer: null, hardTimer: null, forceTimer: null,
        killTimer: null, settleTimer: null, terminationStarted: false,
      };
      active = turn;
      if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
      armStall(turn);
      armHard(turn);
      try {
        startChild(turn, materialized.prompt, materialized.imagePaths);
      } catch (error) {
        markFailure(turn, 'spawn_error', String(error));
        alive = false;
        if (activeChild?.pid) beginTermination(turn, 'SIGINT');
        else settle(turn, true, true);
      }
    });
  }

  function interrupt(): void {
    if (!active || closed) return;
    active.interrupted = true;
    alive = false;
    beginTermination(active, 'SIGINT');
  }

  function close(): void {
    if (!closed) {
      closed = true;
      alive = false;
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = null;
    }
    if (active) {
      const turn = active;
      turn.interrupted = true;
      beginTermination(turn, 'SIGINT');
      return;
    }
    if (activeChild) return;
    cleanupImages();
    cleanupScratch();
    resolveIdle();
  }

  armIdle();
  return {
    sendTurn,
    interrupt,
    close,
    onEvent: (listener) => { listeners.push(listener); },
    touch: armIdle,
    waitForIdle: () => {
      if (!active && !activeChild) {
        if (closed && !scratchRemoved) cleanupScratch();
        return Promise.resolve();
      }
      return new Promise<void>((resolve) => idleWaiters.push(resolve));
    },
    activeTurnProgress: () => active ? {
      startedAtMs: active.startedAtMs,
      lastActivityAtMs: active.lastActivityAtMs,
      toolCallCount: active.toolCallCount,
    } : null,
    sessionId: () => sessionIdValue,
    isAlive: () => alive && !closed,
    exitCode: () => lastExitCode,
    failureCategory: () => terminalFailure ?? classify(failureTail),
    exitDiagnostic: () => alive ? null : [
      `exit_code=${lastExitCode ?? 'null'}`,
      activeChild?.pid ? `active_child_pid=${activeChild.pid}` : '',
      failureTail.trim() ? `provider_output_tail=${sanitize(failureTail).trim().slice(-2_000)}` : '',
    ].filter(Boolean).join('; '),
  };
}
