import { spawn } from 'child_process';
import fs from 'fs';
import net from 'net';
import path from 'path';
import { fileURLToPath } from 'url';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import {
  recordLlmCallStart, recordLlmTokens,
  recordNumTurns, recordProviderReportedUsage, sanitize,
} from '../index.js';
import {
  DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS,
  PROCESS_KILL_GRACE_MS,
} from '../constants.js';
import {
  RESIDENT_PROJECT_READ_TOOL_NAME,
  RESIDENT_REPLY_TOOL_NAME,
  RESIDENT_STRATEGIC_TOOL_NAME,
} from './resident-reply-tool.js';
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
const MCP_BROKER_DRAIN_MS = 1_000;
const OPERATOR_REPLY_TYPES = new Set(['NOTE', 'QUESTION', 'FEEDBACK', 'BUG_REPORT', 'BLOCKER']);
const REPLY_ACCEPTED_TEXT = 'Workday reply accepted for delivery.';
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const RESIDENT_LANDLOCK_EXEC = path.resolve(
  moduleDir,
  '../_agent_scripts/resident_landlock_exec.py',
);
const RESIDENT_MCP_SOCKET_CLIENT = path.resolve(
  moduleDir,
  '../_agent_scripts/resident_mcp_socket_client.mjs',
);
const REQUIRED_READ_ONLY_TOOLS = new Set(['Write', 'Edit', 'NotebookEdit']);
const SAFE_MCP_ENV_KEYS = new Set([
  'RESIDENT_REPLY_RECEIPT_PATH',
  'RESIDENT_STAGED_REPLY_PATH',
  'CALL_RESIDENT_ROLE',
  'CALL_LOCAL_STAGE_DIR',
  'CALL_TURN_RECEIPT_PATH',
  'RESIDENT_STRATEGIC_HELPER_PATH',
  'RESIDENT_STRATEGIC_OPS_PATH',
  'RESIDENT_PROJECT_READ_HELPER_PATH',
  'RESIDENT_PROJECT_READS_PATH',
  'RESIDENT_STRATEGIC_PATH',
  'RESIDENT_STRATEGIC_LANG',
  'RESIDENT_STRATEGIC_TERM',
  'ORCHESTRATOR_URL',
  'AGENT_TOKEN_FILE',
  'BLUEPRINT_RESIDENT_INCARNATION_ID',
  'BLUEPRINT_RESIDENT_REPLY_KEY_PATH',
  'BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH',
  'BLUEPRINT_RESIDENT_STAGED_REPLY_PATH',
  'BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH',
]);
const CODEX_PROCESS_ENV_KEYS = new Set([
  'PATH', 'LANG', 'LC_ALL', 'TERM', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
]);
const CODEX_RESIDENT_BOUNDARY = [
  'Codex Resident runs without native shell, repository, Git, or local file tools.',
  'Do not invoke ./strategic, ./fetch, ./design-doc, ./messages, apply_patch, or any command path.',
  'Treat every shell, file, Git, repository, and ./helper instruction elsewhere as Claude-only legacy vocabulary; it never authorizes a local tool.',
  `Translate supported ./fetch intent to ${RESIDENT_PROJECT_READ_TOOL_NAME} and supported ./strategic intent to ${RESIDENT_STRATEGIC_TOOL_NAME} using exact JSON and receipt semantics.`,
  `The ${RESIDENT_STRATEGIC_TOOL_NAME} data description embeds required and MCP-supported top-level fields, conditional/nested requirements, and valid examples; it replaces ./strategic help/schema. set-running-update sends data {"markdown":"..."} directly, never a file.`,
  'When a requested surface has no native Workday MCP contract, rely on the supplied canonical snapshot and report the limitation; never probe locally.',
  `Use ${RESIDENT_STRATEGIC_TOOL_NAME} for strategic mutations and pass exact JSON values directly.`,
  `Use ${RESIDENT_PROJECT_READ_TOOL_NAME} for bounded canonical precondition checks and readback.`,
  `Use ${RESIDENT_REPLY_TOOL_NAME} only for the complete operator-facing reply.`,
  'Reuse a stable idempotency_key only for a byte-identical strategic retry; after ambiguity, reconcile canonically before retrying.',
  'Native local writes are denied; trusted Workday MCP helpers alone use the private Resident scratch for bounded staging.',
].join(' ');

const FORCED_DISABLED_FEATURES = [
  'shell_tool', 'unified_exec', 'apply_patch_freeform', 'apps', 'browser_use', 'browser_use_external',
  'browser_use_full_cdp_access', 'computer_use', 'goals', 'hooks',
  'image_generation', 'in_app_browser', 'multi_agent', 'plugins',
  'remote_plugin', 'skill_mcp_dependency_install', 'skill_search',
  'tool_suggest', 'view_image', 'auth_elicitation',
  'tool_call_mcp_elicitation', 'code_mode', 'code_mode_host',
] as const;

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

function resolveExecutable(command: string, env: NodeJS.ProcessEnv): string {
  const candidates = path.isAbsolute(command)
    ? [command]
    : (env.PATH ?? '/usr/local/bin:/usr/bin:/bin')
      .split(path.delimiter)
      .filter(Boolean)
      .map((entry) => path.join(entry, command));
  for (const candidate of candidates) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      const resolved = fs.realpathSync(candidate);
      if (fs.statSync(resolved).isFile()) return resolved;
    } catch { /* keep searching the trusted runner PATH */ }
  }
  throw new Error('Codex Resident command is not an executable on the trusted runner PATH');
}

function existingCanonicalPaths(values: readonly string[]): string[] {
  const result: string[] = [];
  for (const value of values) {
    try {
      const resolved = fs.realpathSync(value);
      if (!result.includes(resolved)) result.push(resolved);
    } catch { /* optional system path is absent in this runner image */ }
  }
  return result;
}

function executableRuntimeRoots(executable: string): string[] {
  const marker = `${path.sep}node_modules${path.sep}`;
  const markerAt = executable.indexOf(marker);
  if (markerAt < 0) return [executable];
  const segments = executable.slice(markerAt + marker.length).split(path.sep);
  const packageSegments = segments[0]?.startsWith('@') ? segments.slice(0, 2) : segments.slice(0, 1);
  return [
    executable,
    executable.slice(0, markerAt + marker.length) + packageSegments.join(path.sep),
  ];
}

function replaceFileAtomically(target: string, contents: Buffer): void {
  const temporary = `${target}.resident-${process.pid}-${Math.random().toString(16).slice(2)}`;
  try {
    fs.writeFileSync(temporary, contents, { flag: 'wx', mode: 0o600 });
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* best-effort rollback */ }
    throw error;
  }
}

function tomlStringArray(values: readonly string[]): string {
  return `[${values.map(tomlString).join(',')}]`;
}

function safeConfigKey(value: string, label: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error(`invalid ${label} in Resident MCP config`);
  return value;
}

interface ResidentMcpBrokerSpec {
  name: string;
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
  socketPath: string;
}

function codexMcpArgs(
  opts: ResidentSessionOptions,
  socketRoot: string,
): { args: string[]; brokers: ResidentMcpBrokerSpec[] } {
  if (!opts.replyTool) return { args: ['-c', 'mcp_servers={}'], brokers: [] };
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
  const brokers: ResidentMcpBrokerSpec[] = [];
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
    const upstreamArgs: string[] = [];
    const serverArgs = config.args;
    if (serverArgs !== undefined) {
      if (!Array.isArray(serverArgs) || serverArgs.some((item) => typeof item !== 'string')) {
        throw new Error(`Resident MCP server ${serverName} args must be strings`);
      }
      upstreamArgs.push(...serverArgs as string[]);
    }
    const upstreamEnv: NodeJS.ProcessEnv = {};
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
        upstreamEnv[safeConfigKey(envName, 'environment key')] = envValue;
      }
    }
    const socketPath = path.join(
      socketRoot,
      `blueprint-resident-mcp-${process.pid}-${Math.random().toString(16).slice(2)}-${serverKey}.sock`,
    );
    brokers.push({
      name: serverKey,
      command: config.command,
      args: upstreamArgs,
      env: upstreamEnv,
      socketPath,
    });
    args.push(
      '-c', `mcp_servers.${serverKey}.command=${tomlString(process.execPath)}`,
      '-c', `mcp_servers.${serverKey}.args=${tomlStringArray([RESIDENT_MCP_SOCKET_CLIENT, socketPath])}`,
    );
    args.push(
      '-c', `mcp_servers.${serverKey}.enabled=true`,
      '-c', `mcp_servers.${serverKey}.required=true`,
      '-c', `mcp_servers.${serverKey}.enabled_tools=${tomlStringArray(enabledTools)}`,
      '-c', `mcp_servers.${serverKey}.default_tools_approval_mode=${tomlString('approve')}`,
    );
  }
  return { args, brokers };
}

function normalizeCommonArgs(
  base: RunnerCommand,
  opts: ResidentSessionOptions,
  scratchDir: string,
  socketRoot: string,
): { args: string[]; brokers: ResidentMcpBrokerSpec[] } {
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
      if (arg === '--enable' || arg === '--disable') {
        if (value === 'use_legacy_landlock' || FORCED_DISABLED_FEATURES.some((feature) => feature === value)) {
          index += 1;
          continue;
        }
      }
      if (arg === '-c' || arg === '--config') {
        const key = value.split('=', 1)[0];
        if (
          key === 'sandbox_mode' || key === 'approval_policy'
          || key === 'default_permissions' || key.startsWith('permissions.')
          || key.startsWith('sandbox_workspace_write.')
          || key === 'features.use_legacy_landlock'
          || FORCED_DISABLED_FEATURES.some((feature) => key === `features.${feature}`)
        ) {
          throw new Error(`Codex Resident runner cannot override protected config ${key}`);
        }
      }
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
  const mcp = codexMcpArgs(opts, socketRoot);
  common.push(...mcp.args);
  // Append these last so provider translation cannot broaden the Resident.
  // The checked-in direct Landlock launcher is the sole inner filesystem
  // authority. Codex's native sandbox is explicitly bypassed so it cannot
  // attempt a nested namespace inside the runner's existing outer bwrap.
  common.push(
    '--dangerously-bypass-approvals-and-sandbox',
    ...FORCED_DISABLED_FEATURES.flatMap((feature) => ['--disable', feature]),
    '-c', 'approval_policy="never"',
    '-c', 'web_search="disabled"',
    '-c', 'tools.web_search=false',
    '-c', 'shell_environment_policy.inherit="none"',
    '-c', 'shell_environment_policy.ignore_default_excludes=false',
    '-c', `shell_environment_policy.set.HOME=${tomlString(scratchDir)}`,
  );
  common.push('-c', `shell_environment_policy.set.TMPDIR=${tomlString(scratchDir)}`);
  const developerInstructions = [opts.appendSystemPrompt, CODEX_RESIDENT_BOUNDARY]
    .filter((value): value is string => Boolean(value))
    .join('\n\n');
  common.push('-c', `developer_instructions=${tomlString(developerInstructions)}`);
  return { args: common, brokers: mcp.brokers };
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

function mcpStrategicAcknowledged(result: unknown): boolean {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false;
  const raw = result as Record<string, unknown>;
  const structured = raw.structured_content ?? raw.structuredContent;
  if (!structured || typeof structured !== 'object' || Array.isArray(structured)) return false;
  const receipt = structured as Record<string, unknown>;
  return receipt.outcome === 'applied'
    && receipt.applied === true
    && receipt.commit_acknowledged === true;
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
  const isolation = opts.codexIsolation;
  if (!isolation || isolation.readwritePaths.length !== 1) {
    throw new Error('Codex Resident requires one exact direct-Landlock scratch root');
  }
  if (!fs.existsSync(RESIDENT_LANDLOCK_EXEC) || !fs.existsSync(RESIDENT_MCP_SOCKET_CLIENT)) {
    throw new Error('Codex Resident direct-Landlock runtime is unavailable');
  }
  const allowedMcpTools = new Set(
    (opts.replyTool?.allowedTools ?? [opts.replyTool?.toolName ?? RESIDENT_REPLY_TOOL_NAME]),
  );
  const turnTimeoutMs = opts.turnTimeoutMs ?? DEFAULT_TURN_STALL_MS;
  const hardTurnTimeoutMs = opts.hardTurnTimeoutMs
    ?? Math.max(DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS, turnTimeoutMs + FORCE_SETTLE_MS);
  const idleCeilingMs = opts.idleCeilingMs ?? DEFAULT_IDLE_CEILING_MS;
  const isolationRoot = fs.realpathSync(isolation.readwritePaths[0]);
  const scratchDir = fs.mkdtempSync(path.join(isolationRoot, '.codex-session-'));
  fs.chmodSync(scratchDir, 0o700);
  let mcpSocketRoot = '';
  try {
    mcpSocketRoot = fs.mkdtempSync('/tmp/blueprint-rmcp-');
    fs.chmodSync(mcpSocketRoot, 0o700);
  } catch (error) {
    fs.rmSync(scratchDir, { recursive: true, force: true });
    throw error;
  }
  const rollbackSetup = (): void => {
    fs.rmSync(scratchDir, { recursive: true, force: true });
    fs.rmSync(mcpSocketRoot, { recursive: true, force: true });
  };
  const mergedEnv = { ...runnerBase.env, ...opts.env };
  let codexExecutable: string;
  try {
    codexExecutable = resolveExecutable(runnerBase.command, mergedEnv);
  } catch (error) {
    rollbackSetup();
    throw error;
  }
  const childEnv: NodeJS.ProcessEnv = {
    HOME: scratchDir,
    TMPDIR: scratchDir,
  };
  for (const [envName, envValue] of Object.entries(mergedEnv)) {
    if (CODEX_PROCESS_ENV_KEYS.has(envName)) childEnv[envName] = envValue;
  }
  childEnv.PATH ??= '/usr/local/bin:/usr/bin:/bin';
  let sourceOAuthAuthPath: string | null = null;
  let scratchOAuthAuthPath: string | null = null;
  try {
    if (runner.authMode === 'oauth') {
      const sourceHome = mergedEnv.CODEX_HOME;
      if (!sourceHome || !path.isAbsolute(sourceHome)) {
        throw new Error('OAuth Codex Resident requires an absolute authoritative CODEX_HOME');
      }
      if (!isolation.persistOAuthCredential) {
        throw new Error('OAuth Codex Resident requires fenced credential persistence');
      }
      sourceOAuthAuthPath = path.join(sourceHome, 'auth.json');
      if (!fs.statSync(sourceOAuthAuthPath).isFile()) {
        throw new Error('OAuth Codex Resident authoritative auth.json is unavailable');
      }
      const scratchCodexHome = path.join(scratchDir, 'codex-home');
      fs.mkdirSync(scratchCodexHome, { mode: 0o700 });
      scratchOAuthAuthPath = path.join(scratchCodexHome, 'auth.json');
      fs.copyFileSync(sourceOAuthAuthPath, scratchOAuthAuthPath, fs.constants.COPYFILE_EXCL);
      fs.chmodSync(scratchOAuthAuthPath, 0o600);
      childEnv.CODEX_HOME = scratchCodexHome;
    } else {
      if (mergedEnv.OPENAI_API_KEY) childEnv.OPENAI_API_KEY = mergedEnv.OPENAI_API_KEY;
      if (mergedEnv.CODEX_API_KEY) childEnv.CODEX_API_KEY = mergedEnv.CODEX_API_KEY;
    }
  } catch (error) {
    rollbackSetup();
    throw error;
  }
  let commonArgs: string[];
  let mcpBrokerSpecs: ResidentMcpBrokerSpec[];
  try {
    const normalized = normalizeCommonArgs(runnerBase, opts, scratchDir, mcpSocketRoot);
    commonArgs = normalized.args;
    mcpBrokerSpecs = normalized.brokers;
  } catch (error) {
    rollbackSetup();
    throw error;
  }
  const readonlyPaths = existingCanonicalPaths([
    '/usr', '/usr/local', '/bin', '/sbin', '/lib', '/lib64', '/etc',
    '/dev/urandom', '/dev/random',
    ...isolation.readonlyPaths,
    ...executableRuntimeRoots(codexExecutable),
    ...executableRuntimeRoots(process.execPath),
    RESIDENT_MCP_SOCKET_CLIENT,
    ...(opts.replyTool ? [opts.replyTool.mcpConfigPath] : []),
  ]);
  const denyReadPaths = existingCanonicalPaths([
    ...isolation.denyReadPaths,
    ...(sourceOAuthAuthPath ? [sourceOAuthAuthPath] : []),
  ]);
  const denyWritePaths = existingCanonicalPaths([
    ...isolation.denyWritePaths,
    ...isolation.readonlyPaths,
    ...(sourceOAuthAuthPath ? [sourceOAuthAuthPath] : []),
  ]);
  const landlockPolicy = JSON.stringify({
    version: 1,
    readonly_paths: readonlyPaths,
    // Keep trusted MCP staging/receipt authority outside Codex's fresh HOME/CWD.
    readwrite_paths: [scratchDir],
    deny_read_paths: denyReadPaths,
    deny_write_paths: denyWritePaths,
  });
  let landlockPython: string;
  try {
    landlockPython = resolveExecutable('python3', { PATH: childEnv.PATH });
  } catch (error) {
    rollbackSetup();
    throw error;
  }
  const listeners: Array<(event: ResidentSessionEvent) => void> = [];
  const idleWaiters: Array<() => void> = [];
  let sessionIdValue: string | null = null;
  let alive = true;
  let closed = false;
  let active: CodexTurn | null = null;
  let activeChild: ReturnType<typeof spawn> | null = null;
  let finalizingClosedChild: CodexTurn | null = null;
  const oauthPersistenceByGeneration = new Map<number, Promise<void>>();
  let childGeneration = 0;
  let lastExitCode: number | null = null;
  let failureTail = '';
  let terminalFailure: 'rate_limited' | 'dead_auth' | null = null;
  let idleTimer: NodeJS.Timeout | null = null;
  const imagePaths = new Set<string>();
  const mcpBrokerServers: net.Server[] = [];
  const mcpBrokerSockets = new Set<net.Socket>();
  const mcpBrokerChildren = new Set<ReturnType<typeof spawn>>();
  const mcpBrokerTermTimers = new Map<ReturnType<typeof spawn>, NodeJS.Timeout>();
  const mcpBrokerKillTimers = new Map<ReturnType<typeof spawn>, NodeJS.Timeout>();
  let mcpBrokersShuttingDown = false;
  let mcpBrokerFailure: Error | null = null;
  let mcpBrokerDrainTimer: NodeJS.Timeout | null = null;
  let mcpBrokersReady = mcpBrokerSpecs.length === 0;
  let mcpBrokersReadyObserved: Promise<void> = Promise.resolve();
  let pendingBrokerSettle: {
    turn: CodexTurn; processGone: boolean; childConfirmedGone: boolean;
  } | null = null;
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
  const failMcpBroker = (name: string, error: unknown, reason = 'mcp_broker_failed'): void => {
    const detail = `Resident MCP broker ${name} failed: ${sanitize(String(error)).slice(-1_000)}`;
    mcpBrokerFailure = error instanceof Error ? error : new Error(detail);
    failureTail = boundedTail(failureTail, `\n${detail}`);
    alive = false;
    if (active) {
      active.appliedStrategicOp = false;
      active.sentOperatorMessage = false;
      active.operatorReply = null;
      if (!active.providerFailed) markFailure(active, reason, detail);
      if (!pendingBrokerSettle) beginTermination(active, 'SIGTERM');
    } else {
      closed = true;
      cleanupScratch();
      resolveIdle();
    }
  };
  const reconcileBrokerDrain = (): boolean => {
    if (mcpBrokerChildren.size > 0 || !pendingBrokerSettle) return false;
    if (mcpBrokerDrainTimer) clearTimeout(mcpBrokerDrainTimer);
    mcpBrokerDrainTimer = null;
    const pending = pendingBrokerSettle;
    pendingBrokerSettle = null;
    settle(pending.turn, pending.processGone, pending.childConfirmedGone);
    return true;
  };
  const terminateMcpBrokerChild = (child: ReturnType<typeof spawn>): void => {
    if (mcpBrokerTermTimers.has(child) || mcpBrokerKillTimers.has(child)) return;
    try { child.kill('SIGTERM'); } catch { /* close/error reconciliation below is authoritative */ }
    const termTimer = setTimeout(() => {
      mcpBrokerTermTimers.delete(child);
      if (!mcpBrokerChildren.has(child)) return;
      try { child.kill('SIGKILL'); } catch { /* close/error reconciliation below is authoritative */ }
      const killTimer = setTimeout(() => {
        mcpBrokerKillTimers.delete(child);
        if (!mcpBrokerChildren.has(child)) return;
        let gone = typeof child.pid !== 'number';
        if (typeof child.pid === 'number') {
          try { process.kill(child.pid, 0); } catch (error) {
            const code = error && typeof error === 'object' && 'code' in error
              ? String((error as { code?: unknown }).code) : '';
            gone = code === 'ESRCH';
          }
          if (gone) unregisterChildPid(child.pid);
        }
        if (gone) {
          mcpBrokerChildren.delete(child);
          if (reconcileBrokerDrain()) return;
          cleanupScratch();
          resolveIdle();
          return;
        }
        scratchSafeToRemove = false;
        failureTail = boundedTail(failureTail, '\nresident_mcp_broker_remained_live_after_sigkill');
        if (pendingBrokerSettle) {
          const pending = pendingBrokerSettle;
          pendingBrokerSettle = null;
          alive = false;
          markFailure(
            pending.turn,
            'mcp_broker_kill_failed',
            'Resident MCP broker remained live after SIGKILL grace',
          );
          settle(pending.turn, false, false);
        }
      }, PROCESS_KILL_GRACE_MS);
      mcpBrokerKillTimers.set(child, killTimer);
    }, PROCESS_KILL_GRACE_MS);
    mcpBrokerTermTimers.set(child, termTimer);
  };
  const shutdownMcpBrokers = (): void => {
    if (mcpBrokersShuttingDown) return;
    mcpBrokersShuttingDown = true;
    for (const server of mcpBrokerServers) {
      try { server.close(); } catch { /* not yet listening or already closed */ }
    }
    for (const socket of mcpBrokerSockets) socket.destroy();
    for (const child of mcpBrokerChildren) terminateMcpBrokerChild(child);
    for (const spec of mcpBrokerSpecs) {
      try { fs.unlinkSync(spec.socketPath); } catch { /* absent or already removed */ }
    }
  };
  const cleanupScratch = (): boolean => {
    if (scratchRemoved) return true;
    shutdownMcpBrokers();
    if (mcpBrokerChildren.size > 0) return false;
    try { fs.rmSync(mcpSocketRoot, { recursive: true, force: true }); } catch { /* already revoked */ }
    if (active || activeChild || finalizingClosedChild) return false;
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
  const persistOAuthFromScratch = async (): Promise<void> => {
    if (!sourceOAuthAuthPath || !scratchOAuthAuthPath) return;
    const scratchCredential = fs.readFileSync(scratchOAuthAuthPath);
    let authoritative = fs.readFileSync(sourceOAuthAuthPath);
    if (scratchCredential.equals(authoritative)) return;
    replaceFileAtomically(sourceOAuthAuthPath, scratchCredential);
    await isolation.persistOAuthCredential?.();
    authoritative = fs.readFileSync(sourceOAuthAuthPath);
    replaceFileAtomically(scratchOAuthAuthPath, authoritative);
  };
  const persistOAuthForGeneration = (generation: number): Promise<void> => {
    const existing = oauthPersistenceByGeneration.get(generation);
    if (existing) return existing;
    const attempt = Promise.resolve().then(persistOAuthFromScratch);
    oauthPersistenceByGeneration.set(generation, attempt);
    return attempt;
  };
  const resolveIdle = (): void => {
    if (active || activeChild || mcpBrokerChildren.size > 0) return;
    while (idleWaiters.length) idleWaiters.shift()?.();
  };
  const startMcpBrokers = (): Promise<void> => {
    const trustedRoots = isolation.readonlyPaths.map((root) => fs.realpathSync(root));
    const trustedNode = fs.realpathSync(process.execPath);
    const activeNames = new Set<string>();
    const readiness: Promise<void>[] = [];
    for (const spec of mcpBrokerSpecs) {
      const upstreamExecutable = resolveExecutable(spec.command, mergedEnv);
      if (upstreamExecutable !== trustedNode || spec.args.length !== 1) {
        throw new Error(`Resident MCP broker ${spec.name} must launch one exact trusted Node script`);
      }
      const upstreamScript = fs.realpathSync(spec.args[0]);
      const relativeToTrustedRoot = trustedRoots.some((root) => {
        const relative = path.relative(root, upstreamScript);
        return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
      });
      const scriptInfo = fs.statSync(upstreamScript);
      if (
        !relativeToTrustedRoot || !scriptInfo.isFile()
        || scriptInfo.mode & 0o022 || path.extname(upstreamScript) !== '.mjs'
      ) {
        throw new Error(`Resident MCP broker ${spec.name} script is outside the immutable generation`);
      }
      const server = net.createServer((socket) => {
        if (mcpBrokersShuttingDown || activeNames.has(spec.name)) {
          socket.destroy();
          return;
        }
        activeNames.add(spec.name);
        mcpBrokerSockets.add(socket);
        const upstreamEnv: NodeJS.ProcessEnv = {
          HOME: scratchDir,
          TMPDIR: scratchDir,
        };
        for (const [key, value] of Object.entries(mergedEnv)) {
          if (CODEX_PROCESS_ENV_KEYS.has(key)) upstreamEnv[key] = value;
        }
        upstreamEnv.PATH ??= '/usr/local/bin:/usr/bin:/bin';
        Object.assign(upstreamEnv, spec.env);
        let upstream: ReturnType<typeof spawn>;
        try {
          upstream = spawn(upstreamExecutable, [upstreamScript], {
            cwd: scratchDir, env: upstreamEnv, stdio: ['pipe', 'pipe', 'pipe'],
          });
        } catch (error) {
          activeNames.delete(spec.name);
          socket.destroy();
          failMcpBroker(spec.name, error);
          return;
        }
        const upstreamInput = upstream.stdin;
        const upstreamOutput = upstream.stdout;
        const upstreamError = upstream.stderr;
        if (!upstreamInput || !upstreamOutput || !upstreamError) {
          try { upstream.kill('SIGKILL'); } catch { /* launch reconciliation owns cleanup */ }
          activeNames.delete(spec.name);
          socket.destroy();
          failMcpBroker(spec.name, 'trusted upstream stdio was not piped');
          return;
        }
        mcpBrokerChildren.add(upstream);
        if (typeof upstream.pid === 'number') registerChildPid(upstream.pid);
        let upstreamStderr = '';
        upstreamError.on('data', (data: Buffer) => {
          upstreamStderr = boundedTail(upstreamStderr, sanitize(data.toString()), 2_000);
        });
        upstreamInput.on('error', () => { socket.destroy(); });
        socket.on('error', () => { /* close below fences the exact upstream */ });
        socket.pipe(upstreamInput);
        upstreamOutput.pipe(socket);
        socket.once('close', () => {
          mcpBrokerSockets.delete(socket);
          // EOF fences new requests but lets an already-started typed helper
          // reach its own receipt/timeout boundary. Forced session teardown
          // signals the MCP parent, whose checked-in handler kills its helper
          // process group before exiting.
          try { upstreamInput.end(); } catch { /* already drained */ }
        });
        upstream.once('error', (error: Error) => {
          failMcpBroker(spec.name, error);
          socket.destroy();
        });
        upstream.once('close', (code: number | null) => {
          const termTimer = mcpBrokerTermTimers.get(upstream);
          if (termTimer) clearTimeout(termTimer);
          mcpBrokerTermTimers.delete(upstream);
          const killTimer = mcpBrokerKillTimers.get(upstream);
          if (killTimer) clearTimeout(killTimer);
          mcpBrokerKillTimers.delete(upstream);
          if (typeof upstream.pid === 'number') unregisterChildPid(upstream.pid);
          mcpBrokerChildren.delete(upstream);
          activeNames.delete(spec.name);
          if (code !== 0 && !mcpBrokersShuttingDown) {
            failMcpBroker(spec.name, upstreamStderr || `exit code ${String(code)}`);
          }
          socket.end();
          if (reconcileBrokerDrain()) return;
          if (!alive || closed) cleanupScratch();
          resolveIdle();
        });
      });
      server.maxConnections = 1;
      mcpBrokerServers.push(server);
      readiness.push(new Promise<void>((resolve, reject) => {
        const startupError = (error: Error): void => { reject(error); };
        server.once('error', startupError);
        server.listen(spec.socketPath, () => {
          try {
            fs.chmodSync(spec.socketPath, 0o600);
            if ((fs.statSync(spec.socketPath).mode & 0o777) !== 0o600) {
              throw new Error('private socket mode is not 0600');
            }
          } catch (error) {
            try { server.close(); } catch { /* startup rejection owns cleanup */ }
            reject(error instanceof Error ? error : new Error(String(error)));
            return;
          }
          server.off('error', startupError);
          server.on('error', (error) => { failMcpBroker(spec.name, error); });
          resolve();
        });
      }));
      server.unref();
    }
    return Promise.all(readiness).then(() => undefined);
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
    if (childConfirmedGone && mcpBrokerChildren.size > 0) {
      activeChild = null;
      clearTimers(turn);
      cleanupImages();
      pendingBrokerSettle = { turn, processGone, childConfirmedGone };
      if (!alive || closed) shutdownMcpBrokers();
      if (!mcpBrokerDrainTimer) {
        mcpBrokerDrainTimer = setTimeout(() => {
          mcpBrokerDrainTimer = null;
          if (active !== turn || !pendingBrokerSettle || mcpBrokerChildren.size === 0) return;
          failMcpBroker('drain', 'upstream did not close after Codex completion', 'mcp_broker_drain_timeout');
          shutdownMcpBrokers();
        }, MCP_BROKER_DRAIN_MS);
      }
      return;
    }
    pendingBrokerSettle = null;
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
  const recordProviderDiagnostic = (turn: CodexTurn, reason: string, detail: string): void => {
    const clean = sanitize(detail).slice(-2_000) || reason;
    failureTail = boundedTail(failureTail, `\n${clean}`);
    trace(turn, 'error', reason, true);
    emit({ kind: 'error', text: clean });
  };
  const settleGoneAfterOAuth = (turn: CodexTurn): void => {
    if (active !== turn || finalizingClosedChild === turn) return;
    activeChild = null;
    clearTimers(turn);
    cleanupImages();
    finalizingClosedChild = turn;
    void persistOAuthForGeneration(turn.childGeneration).then(() => {
      if (active !== turn) return;
      finalizingClosedChild = null;
      settle(turn, true, true);
    }).catch((error: unknown) => {
      if (active !== turn) return;
      finalizingClosedChild = null;
      scratchSafeToRemove = false;
      markFailure(turn, 'oauth_rotation_failed', String(error));
      alive = false;
      settle(turn, true, true);
    });
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
        settle(turn, false, false);
      } else {
        settleGoneAfterOAuth(turn);
      }
      return;
    }
    const escalateToKill = (): void => {
      if (active !== turn) return;
      const killResult = signalTurn(turn, 'SIGKILL');
      if (killResult !== 'delivered') {
        if (killResult === 'failed') {
          scratchSafeToRemove = false;
          markFailure(turn, 'kill_failed', 'Could not deliver SIGKILL to Codex child');
          settle(turn, false, false);
        } else {
          settleGoneAfterOAuth(turn);
        }
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
          settle(turn, false, false);
        } else {
          settleGoneAfterOAuth(turn);
        }
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
            settle(turn, false, false);
          } else {
            settleGoneAfterOAuth(turn);
          }
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
    if (itemType === 'error') {
      if (completed) {
        const envelope = item.error;
        const envelopeMessage = envelope && typeof envelope === 'object' && !Array.isArray(envelope)
          ? (envelope as Record<string, unknown>).message
          : null;
        const message = typeof item.message === 'string' && item.message
          ? item.message
          : typeof envelopeMessage === 'string' && envelopeMessage
            ? envelopeMessage
            : 'Codex provider diagnostic';
        recordProviderDiagnostic(turn, 'item_error', message);
      }
      return;
    }
    const mcpTool = typeof item.tool === 'string' ? item.tool : '';
    const mcpServer = typeof item.server === 'string' ? item.server : '';
    const fullMcpTool = mcpServer && mcpTool ? `mcp__${mcpServer}__${mcpTool}` : '';
    const unexpectedLocal = itemType === 'command_execution'
      || itemType === 'file_change'
      || itemType === 'web_search'
      || itemType === 'todo_list';
    const unexpectedMcp = itemType === 'mcp_tool_call' && !allowedMcpTools.has(fullMcpTool);
    const unknownItem = !['mcp_tool_call', 'reasoning', 'agent_message'].includes(itemType);
    if (unexpectedLocal || unexpectedMcp || unknownItem) {
      if (!turn.providerFailed) {
        markFailure(
          turn,
          unexpectedMcp ? 'unexpected_mcp_tool' : 'unexpected_local_tool',
          unexpectedMcp ? 'Codex attempted a non-allowlisted MCP tool' : 'Codex attempted a forbidden local tool',
        );
      }
      turn.sentOperatorMessage = false;
      turn.operatorReply = null;
      alive = false;
      beginTermination(turn, 'SIGINT');
      return;
    }
    const toolName = itemType === 'command_execution' ? 'Bash'
      : itemType === 'file_change' ? 'apply_patch'
        : itemType === 'mcp_tool_call' ? `MCP:${mcpTool}`
          : itemType === 'web_search' ? 'WebSearch'
            : itemType === 'todo_list' ? 'TodoWrite' : null;
    if (!toolName) return;
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
      if (itemType === 'mcp_tool_call' && started) {
        const server = item.server;
        const tool = item.tool;
        if (server === 'workday' && tool === 'reply_to_operator') {
          const reply = operatorReply(item.arguments);
          if (reply) turn.pendingReplies.set(id, reply);
        }
        if (server === 'workday' && tool === 'strategic_operation') {
          turn.pendingStrategic.add(id);
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
    if (
      turn.pendingStrategic.delete(id)
      && succeeded
      && item.error == null
      && mcpStrategicAcknowledged(item.result)
    ) {
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
      recordProviderDiagnostic(turn, 'provider_error', message);
    }
  };

  const startChild = (turn: CodexTurn, prompt: string, turnImages: string[]): void => {
    const resume = sessionIdValue !== null;
    const codexArgs = resume
      ? ['exec', 'resume', ...commonArgs, ...turnImages.flatMap((image) => ['-i', image]), sessionIdValue as string, '-']
      : ['exec', ...commonArgs, ...turnImages.flatMap((image) => ['-i', image]), '-'];
    const child = spawn(landlockPython, [
      '-I', RESIDENT_LANDLOCK_EXEC,
      '--policy-json', landlockPolicy,
      '--', codexExecutable, ...codexArgs,
    ], {
      cwd: scratchDir,
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
    child.on('close', (code: number | null, signal: NodeJS.Signals | null) => { void (async () => {
      if (typeof pid === 'number') unregisterChildPid(pid);
      if (active !== turn || turn.childGeneration !== generation) {
        cleanupImages();
        try {
          await persistOAuthForGeneration(generation);
          scratchSafeToRemove = true;
        } catch (error) {
          scratchSafeToRemove = false;
          failureTail = boundedTail(
            failureTail,
            `\noauth_rotation_failed: ${sanitize(String(error)).slice(-2_000)}`,
          );
        }
        if (activeChild?.pid === pid) activeChild = null;
        if (!alive || closed) cleanupScratch();
        if (!active && !activeChild) resolveIdle();
        return;
      }
      if (carry.trim()) handleLine(turn, carry);
      // The close event is the exact process-reaped boundary. Revoke the PID
      // authority and all process timers before an asynchronous credential CAS;
      // otherwise a timeout or close() could signal a reused PID and settle the
      // turn while the only refreshed credential is still being reconciled.
      if (activeChild?.pid === pid) activeChild = null;
      clearTimers(turn);
      cleanupImages();
      finalizingClosedChild = turn;
      lastExitCode = code;
      const cleanExit = code === 0 && signal === null;
      const validThread = turn.threadReceiptValid && sessionIdValue !== null;
      let success = cleanExit && turn.terminalCompleted && !turn.terminalFailed && !turn.providerFailed && validThread;
      try {
        await persistOAuthForGeneration(generation);
      } catch (error) {
        scratchSafeToRemove = false;
        markFailure(turn, 'oauth_rotation_failed', String(error));
        success = false;
      }
      if (!success && !turn.providerFailed && !turn.interrupted) {
        const reason = stderr.includes('resident_landlock:') ? 'resident_landlock_failed'
          : !validThread ? 'missing_thread_id'
          : !turn.terminalCompleted ? 'missing_turn_completed'
            : !cleanExit ? 'process_exit' : 'terminal_failure';
        markFailure(
          turn,
          reason,
          failureTail.trim() || stderr || `Codex exited code=${String(code)} signal=${String(signal)}`,
        );
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
      finalizingClosedChild = null;
      settle(turn, turn.interrupted || !success, true);
    })().catch((error: unknown) => {
      if (active !== turn || turn.childGeneration !== generation) return;
      finalizingClosedChild = null;
      scratchSafeToRemove = false;
      markFailure(turn, 'oauth_rotation_failed', String(error));
      alive = false;
      settle(turn, true, true);
    }); });
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
        timedOut: false, hardTimedOut: false, providerFailed: mcpBrokerFailure !== null,
        failureCategory: terminalFailure,
        failureDetail: mcpBrokerFailure ? sanitize(mcpBrokerFailure.message).slice(-2_000) : null,
        providerTerminalReason: mcpBrokerFailure ? 'mcp_broker_failed' : null,
        providerOutputTail: mcpBrokerFailure ? sanitize(mcpBrokerFailure.message).slice(-2_000) : null,
        sawToolUse: false, appliedStrategicOp: false,
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
      const launch = (): void => {
        if (active !== turn || mcpBrokerFailure) return;
        try {
          startChild(turn, materialized.prompt, materialized.imagePaths);
        } catch (error) {
          markFailure(turn, 'spawn_error', String(error));
          alive = false;
          if (activeChild?.pid) beginTermination(turn, 'SIGINT');
          else settle(turn, true, true);
        }
      };
      if (mcpBrokersReady) launch();
      else void mcpBrokersReadyObserved.then(launch);
    });
  }

  function interrupt(): void {
    if (!active || closed) return;
    if (finalizingClosedChild === active) {
      alive = false;
      return;
    }
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
      if (finalizingClosedChild === active) return;
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

  try {
    mcpBrokersReadyObserved = startMcpBrokers().then(() => {
      mcpBrokersReady = true;
    }).catch((error: unknown) => {
      failMcpBroker('startup', error);
    });
  } catch (error) {
    alive = false;
    closed = true;
    cleanupScratch();
    throw error;
  }
  armIdle();
  return {
    sendTurn,
    interrupt,
    close,
    onEvent: (listener) => { listeners.push(listener); },
    touch: armIdle,
    waitForIdle: () => {
      if (!active && !activeChild && mcpBrokerChildren.size === 0) {
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
    privateRuntimeCleanupSafe: () => scratchSafeToRemove,
    exitDiagnostic: () => alive ? null : [
      `exit_code=${lastExitCode ?? 'null'}`,
      activeChild?.pid ? `active_child_pid=${activeChild.pid}` : '',
      failureTail.trim() ? `provider_output_tail=${sanitize(failureTail).trim().slice(-2_000)}` : '',
    ].filter(Boolean).join('; '),
  };
}
