import fs from 'fs';
import { spawn } from 'child_process';
import type WebSocket from 'ws';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import { getCheckRunawayStatus, isCheckInvocation, recordCheckOutcome } from '../fix-loop-events.js';
import {
  CHECK_RUNAWAY_CONSECUTIVE_FAILURES, CHECK_RUNAWAY_MIN_INVOCATIONS, CLAUDE_RESULT_GRACE_MS,
  CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS, LOG_TAIL_BYTES,
  MAX_CLAUDE_CHECK_INVOCATIONS, MAX_OUTPUT_BYTES,
  PROCESS_KILL_GRACE_MS, STDERR_TAIL_BYTES, TOOL_USE_STALL_TIMEOUT_MS,
  WORKFLOW_TOOL_STALL_TIMEOUT_MS, isWorkflowToolName,
  WITHIN_CHUNK_YIELD_LINES, WITHIN_CHUNK_YIELD_MS, claudeTimeoutForEffort,
} from '../constants.js';
import {
  WORKSPACE, ClaudeTimeoutError, getAgentToken, log, recordLlmCallStart,
  recordLlmTokens, recordToolUse, sanitize, sendLog, strVal, estimateTokensFromChars,
} from '../index.js';

export type ProviderName = 'anthropic' | 'openai' | 'gemini';
export type AuthMode = 'oauth' | 'api_key' | 'service_account';
export type OutputMode = 'claude-stream-json' | 'codex-jsonl' | 'text';

const RATE_LIMIT_PATTERNS: Readonly<Record<ProviderName, Partial<Record<AuthMode, readonly string[]>>>> = {
  anthropic: {
    oauth: [
      "you've hit your limit",
      "you've hit your usage limit",
      "you've hit your session limit",
      "you've hit your weekly limit",
      "weekly limit · resets",
      'claude ai usage limit reached',
      "you're out of extra usage",
      '5-hour limit reached',
      'api error: rate limit reached',
      "this request would exceed your account's rate limit",
    ],
    api_key: [
      "you've hit your limit",
      "you've hit your usage limit",
      "you've hit your session limit",
      "you've hit your weekly limit",
      "weekly limit · resets",
      'claude ai usage limit reached',
      "you're out of extra usage",
      '5-hour limit reached',
      'api error: rate limit reached',
      "this request would exceed your account's rate limit",
    ],
  },
  openai: {
    oauth: [
      "you've hit your usage limit",
      "you've hit rate limit",
      'exceeded retry limit',
      'last status: 429',
      'insufficient_quota',
      'usage_not_included',
      'rate_limit_exceeded',
    ],
    api_key: [
      'quota exceeded. check your plan and billing',
      'rate limit reached for',
      'please try again in',
    ],
  },
  gemini: {
    api_key: [
      'rate limit exceeded',
      "you've hit the api rate limit",
      'resource has been exhausted',
      'resource_exhausted',
      'please wait and try again later',
      'you have reached your daily',
      'automatically switching from gemini-',
      'quota exceeded for quota metric',
      'quota exceeded for metric',
      "you've exceeded the rate limit",
      'you exceeded your current quota, please check your plan and billing details',
      'please retry in',
      'to increase your limits, request a quota increase',
      'switching to the gemini-2.5-flash model',
    ],
    service_account: [
      'resource has been exhausted',
      'resource_exhausted',
      'ratelimitexceeded',
      'quota exceeded for metric',
      'resource exhausted. please try again later',
      'please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429',
    ],
  },
} as const;

function normalizeProvider(raw: string | undefined): ProviderName {
  if (raw === 'openai' || raw === 'gemini') return raw;
  return 'anthropic';
}

function normalizeAuthMode(raw: string | undefined, provider: ProviderName): AuthMode {
  if (provider === 'gemini' && raw === 'service_account') return raw;
  if (raw === 'api_key' || raw === 'oauth') return raw;
  if (provider === 'gemini') return 'api_key';
  return 'oauth';
}

export let PROVIDER: ProviderName = normalizeProvider(process.env.PROVIDER);
export let AUTH_MODE: AuthMode = normalizeAuthMode(process.env.AUTH_MODE, PROVIDER);
export let LLM_EXECUTION_MODE = process.env.LLM_EXECUTION_MODE || 'default';

export type LLMCliTranslation = {
  env: Record<string, string>;
  args: string[];
  prompt_fragments: string[];
  unsupported: string[];
};

export function parseLLMCliTranslation(): LLMCliTranslation | null {
  const raw = process.env.LLM_CLI_TRANSLATION;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<LLMCliTranslation>;
    return {
      env: parsed.env ?? {},
      args: Array.isArray(parsed.args) ? parsed.args.map(String) : [],
      prompt_fragments: Array.isArray(parsed.prompt_fragments) ? parsed.prompt_fragments.map(String) : [],
      unsupported: Array.isArray(parsed.unsupported) ? parsed.unsupported.map(String) : [],
    };
  } catch (e) {
    console.warn(`[LLM_SETTINGS] Failed to parse LLM_CLI_TRANSLATION: ${e instanceof Error ? e.message : String(e)}`);
    return null;
  }
}

export let LLM_CLI_TRANSLATION: LLMCliTranslation | null = parseLLMCliTranslation();

export function refreshRuntimeState(): void {
  const provider = normalizeProvider(process.env.PROVIDER);
  PROVIDER = provider;
  AUTH_MODE = normalizeAuthMode(process.env.AUTH_MODE, provider);
  LLM_EXECUTION_MODE = process.env.LLM_EXECUTION_MODE || 'default';
  LLM_CLI_TRANSLATION = parseLLMCliTranslation();
}

function getRateLimitPatterns(provider: ProviderName, authMode: AuthMode): readonly string[] {
  return RATE_LIMIT_PATTERNS[provider][authMode] ?? RATE_LIMIT_PATTERNS[provider].oauth ?? [];
}

const STRUCTURED_429_PATTERNS = ['"api_error_status":429', '"error":"rate_limit"'] as const;

export function matchesRateLimit(text: string, provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): boolean {
  const lower = text.toLowerCase();
  if (getRateLimitPatterns(provider, authMode).some(p => lower.includes(p))) return true;
  return STRUCTURED_429_PATTERNS.some(p => text.includes(p));
}

export class RateLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RateLimitError';
  }
}

// Thrown when the llm-no-progress watchdog fires on a GENUINELY silent call
// (no model deltas for >LLM_NO_PROGRESS_STALL_MS) but NO confirmed rate-limit
// message matched (no RATE_LIMIT_PATTERNS hit). This is a transient provider
// hang of unknown cause, NOT a confirmed account rate-limit: the orchestrator
// must requeue the task (without burning retry) and leave the account AVAILABLE
// rather than blocking its opus model. A confirmed rate-limit message still
// throws RateLimitError (→ block + canonical cooldown), unchanged. Carries
// failureCategory='no_progress_timeout' so the agent's catch block reports the
// transient category instead of 'rate_limited'.
export class NoProgressTimeoutError extends Error {
  readonly failureCategory = 'no_progress_timeout' as const;
  constructor(message: string) {
    super(message);
    this.name = 'NoProgressTimeoutError';
  }
}

interface ClaudeOutputBlock { type: string; text?: string; name?: string; id?: string; input?: unknown }
export interface ClaudeOutput {
  type: string;
  message?: { content: ClaudeOutputBlock[] };
  result?: string;
  // Partial-message envelope emitted by `--include-partial-messages`. Carried on
  // `{"type":"stream_event", ...}` lines; `event.type` is the incremental SSE
  // event (e.g. "content_block_delta" with a text_delta / thinking_delta). Used
  // ONLY as a liveness signal (reset the no-progress watchdog); never decoded for
  // content, so the inner delta shape is intentionally left untyped.
  event?: { type?: string } | null;
  // Present on events emitted by a sub-agent the parent spawned via the
  // Task/Agent/Workflow tool. A non-empty value marks a NESTED event: it is
  // legitimate in-flight sub-progress for the parent's still-running tool, NOT
  // a top-level model turn and NOT a tool-result that clears the parent's tool.
  parent_tool_use_id?: string | null;
}

export interface RunnerCommand {
  command: string;
  args: string[];
  env: Record<string, string>;
  outputMode: OutputMode;
  timeoutMs: number;
  promptInput: 'arg' | 'stdin' | 'stdin-file';
  displayName: string;
}

export interface ValidationCommand {
  command: string;
  args: string[];
  env: Record<string, string>;
  timeoutMs: number;
}

export interface ProviderRunner {
  provider: ProviderName;
  authMode: AuthMode;
  displayName: string;
  credentialSummary(): string;
  buildRunCommand(prompt: string, model?: string, effort?: string, disallowedTools?: string[]): RunnerCommand;
  buildValidationCommand(model?: string): ValidationCommand | null;
  missingCredentials(): string[];
}

function buildBaseCliEnv(): Record<string, string> {
  const env: Record<string, string> = {
    HOME: process.env.HOME || '/home/agent',
    PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: process.env.LANG || 'en_US.UTF-8',
    TERM: process.env.TERM || 'xterm-256color',
  };
  for (const key of [
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'OPENAI_API_KEY',
    'CODEX_API_KEY',
    'CODEX_HOME',
    'GEMINI_API_KEY',
    'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_CLOUD_LOCATION',
    'GOOGLE_GENAI_USE_VERTEXAI',
    'ORCHESTRATOR_URL',
    'TASK_ID',
  ]) {
    const v = process.env[key];
    if (v !== undefined) env[key] = v;
  }
  const agentToken = getAgentToken();
  if (agentToken) env.AGENT_API_TOKEN = agentToken;
  if (LLM_CLI_TRANSLATION?.env) {
    for (const [k, v] of Object.entries(LLM_CLI_TRANSLATION.env)) {
      if (!(k in env)) env[k] = v;
    }
  }
  return env;
}

function withExecutionModePrompt(prompt: string): string {
  const fragments: string[] = [];
  if (LLM_CLI_TRANSLATION?.prompt_fragments.length) {
    fragments.push(...LLM_CLI_TRANSLATION.prompt_fragments);
  }
  if (LLM_EXECUTION_MODE === 'advisor' && !LLM_CLI_TRANSLATION) {
    fragments.push(
      'Execution mode: advisor.',
      'Prioritize analysis, recommendations, and risk assessment.',
      'Avoid making code changes unless the task explicitly requires implementation.',
    );
  }
  if (!fragments.length) return prompt;
  return [...fragments, '', prompt].join('\n');
}

function writePrivateFileSync(path: string, contents: string): void {
  fs.writeFileSync(path, contents, { encoding: 'utf-8', mode: 0o600 });
  try { fs.chmodSync(path, 0o600); } catch { /* best effort on non-POSIX filesystems */ }
}

function appendBoundedTail(current: string, chunk: string, maxBytes: number): string {
  const combined = current + chunk;
  if (Buffer.byteLength(combined, 'utf-8') <= maxBytes) return combined;
  return Buffer.from(combined, 'utf-8').subarray(-maxBytes).toString('utf-8');
}

export function getProviderRunner(provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): ProviderRunner {
  const baseEnv = buildBaseCliEnv();
  if (provider === 'openai') {
    return {
      provider,
      authMode,
      displayName: 'OpenAI Codex',
      credentialSummary: () => authMode === 'oauth' ? 'Using Codex OAuth auth.json' : 'Using OPENAI/CODEX API key auth',
      missingCredentials: () => {
        if (authMode === 'oauth') return baseEnv.CODEX_HOME ? [] : ['CODEX_HOME'];
        return (baseEnv.OPENAI_API_KEY || baseEnv.CODEX_API_KEY) ? [] : ['OPENAI_API_KEY or CODEX_API_KEY'];
      },
      buildRunCommand: (_prompt, model, effort) => {
        const args = ['exec', '--skip-git-repo-check', '--json'];
        if (LLM_CLI_TRANSLATION) {
          args.push(...LLM_CLI_TRANSLATION.args);
        } else {
          args.push('--dangerously-bypass-approvals-and-sandbox');
          if (model) args.push('-m', model);
        }
        return {
          command: 'codex',
          args,
          env: baseEnv,
          outputMode: 'codex-jsonl',
          timeoutMs: claudeTimeoutForEffort(effort),
          promptInput: 'stdin',
          displayName: 'OpenAI Codex',
        };
      },
      buildValidationCommand: (model) => {
        const args = ['exec', '--skip-git-repo-check', '-s', 'danger-full-access'];
        if (model) args.push('-m', model);
        args.push('Respond with OK.');
        return { command: 'codex', args, env: baseEnv, timeoutMs: 30_000 };
      },
    };
  }
  if (provider === 'gemini') {
    return {
      provider,
      authMode,
      displayName: 'Gemini CLI',
      credentialSummary: () => authMode === 'service_account' ? 'Using Gemini Vertex service account auth' : 'Using GEMINI_API_KEY auth',
      missingCredentials: () => {
        if (authMode === 'service_account') return baseEnv.GOOGLE_APPLICATION_CREDENTIALS ? [] : ['GOOGLE_APPLICATION_CREDENTIALS'];
        return baseEnv.GEMINI_API_KEY ? [] : ['GEMINI_API_KEY'];
      },
      buildRunCommand: (_prompt, model, effort) => {
        const args = ['-o', 'text'];
        const translationArgs = LLM_CLI_TRANSLATION?.args ?? [];
        const hasApprovalOverride = translationArgs.some(a => typeof a === 'string' && a.startsWith('--approval-mode'));
        if (!hasApprovalOverride) args.unshift('--approval-mode=yolo');
        if (LLM_CLI_TRANSLATION) {
          args.push(...translationArgs);
        } else if (model) {
          args.push('-m', model);
        }
        const geminiEnv = { ...baseEnv, GEMINI_SANDBOX: 'false', GEMINI_CLI_TRUST_WORKSPACE: 'true' };
        return {
          command: 'gemini',
          args,
          env: geminiEnv,
          outputMode: 'text',
          timeoutMs: claudeTimeoutForEffort(effort),
          promptInput: 'stdin',
          displayName: 'Gemini CLI',
        };
      },
      buildValidationCommand: (model) => {
        const args = ['-o', 'text', '-p', 'Respond with OK.'];
        if (model) args.push('-m', model);
        const geminiEnv = { ...baseEnv, GEMINI_SANDBOX: 'false', GEMINI_CLI_TRUST_WORKSPACE: 'true' };
        return { command: 'gemini', args, env: geminiEnv, timeoutMs: 30_000 };
      },
    };
  }
  return {
    provider: 'anthropic',
    authMode,
    displayName: 'Claude Code',
    credentialSummary: () => baseEnv.CLAUDE_CODE_OAUTH_TOKEN ? 'Using OAuth token for Claude Code auth' : 'Using ANTHROPIC_API_KEY as fallback auth',
    missingCredentials: () => (baseEnv.CLAUDE_CODE_OAUTH_TOKEN || baseEnv.ANTHROPIC_API_KEY) ? [] : ['CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY'],
    buildRunCommand: (_prompt, model, effort, disallowedTools) => {
      // --include-partial-messages emits incremental `stream_event` lines
      // (content_block_delta text / thinking_delta) DURING a model turn. Without
      // it, a long opus turn produces NO stdout event until the COMPLETE
      // assistant message, so `lastStreamEventAt` ages past LLM_NO_PROGRESS_STALL_MS
      // even while the model is actively generating/thinking → false rate_limited
      // requeue + account-model block (a self-inflicted cascade that mimics rate
      // limiting). With partial messages, a slow-but-streaming call keeps the
      // no-progress watchdog fresh (real instance-level progress signal), while a
      // genuinely hung call — a real 429/529 the SDK retries silently, emitting
      // ZERO model deltas — still goes stale and fires correctly.
      const args = ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--dangerously-skip-permissions'];
      let effectiveEffort = effort;
      if (LLM_CLI_TRANSLATION) {
        args.push(...LLM_CLI_TRANSLATION.args);
        const idx = LLM_CLI_TRANSLATION.args.indexOf('--effort');
        if (idx >= 0 && idx + 1 < LLM_CLI_TRANSLATION.args.length) {
          effectiveEffort = LLM_CLI_TRANSLATION.args[idx + 1];
        }
      } else {
        if (model) args.push('--model', model);
        if (effort) args.push('--effort', effort);
      }
      if (disallowedTools && disallowedTools.length > 0) {
        for (const tool of disallowedTools) args.push('--disallowedTools', tool);
      }
      return {
        command: 'claude',
        args,
        env: baseEnv,
        outputMode: 'claude-stream-json',
        timeoutMs: claudeTimeoutForEffort(effectiveEffort),
        promptInput: 'stdin-file',
        displayName: 'Claude Code',
      };
    },
    buildValidationCommand: () => ({
      command: 'claude',
      args: ['-p', 'respond with OK', '--max-turns', '1', '--output-format', 'stream-json', '--dangerously-skip-permissions'],
      env: baseEnv,
      timeoutMs: 30_000,
    }),
  };
}

type RunClaudeCodeOverride = (
  prompt: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean,
) => Promise<{ exitCode: number; allOutput: string; lastResultText: string }>;

let _runClaudeCodeOverride: RunClaudeCodeOverride | null = null;
export function __setRunClaudeCodeOverrideForSmoke(fn: RunClaudeCodeOverride | null): void {
  _runClaudeCodeOverride = fn;
}

export function codexStreamHasTurnCompleted(transcript: string): boolean {
  for (const line of transcript.split('\n')) {
    if (!line.startsWith('{')) continue;
    let obj: Record<string, unknown> | null = null;
    try {
      obj = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (typeof obj.type === 'string' && obj.type === 'turn.completed') {
      return true;
    }
  }
  return false;
}

export async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
  codexTurnCompleted?: boolean;
  stderrTail?: string;
}> {
  if (_runClaudeCodeOverride) {
    return _runClaudeCodeOverride(prompt, ws, statusRef, model, effort, disallowedTools, enableCheckWatchdog);
  }
  return runProviderAgent(prompt, ws, statusRef, model, effort, disallowedTools, enableCheckWatchdog);
}

export async function runProviderAgent(prompt: string, ws: WebSocket | null, statusRef: { value: string }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
  codexTurnCompleted?: boolean;
  stderrTail?: string;
}> {
  const runner = getProviderRunner();
  const runCommand = runner.buildRunCommand(prompt, model, effort, disallowedTools);
  let allOutput = '';
  let lastResultText = '';
  let stderrBuffer = '';
  const flags: { timedOut: boolean; providerKilledByOOM: boolean; stalledMidStream: boolean; llmNoProgressHang: boolean } = { timedOut: false, providerKilledByOOM: false, stalledMidStream: false, llmNoProgressHang: false };
  let lastToolUseAt: number | null = null;
  let toolInFlight = false;
  // True while the in-flight top-level tool is a workflow / sub-agent fan-out
  // (Task/Agent/Workflow). Such tools legitimately run for a long time while
  // streaming nested sub-agent progress, so they get the larger
  // WORKFLOW_TOOL_STALL_TIMEOUT_MS silence budget instead of TOOL_USE_STALL_TIMEOUT_MS.
  let workflowToolInFlight = false;
  // Timestamp of the last sub-progress signal for the in-flight tool. Refreshed
  // by (a) any stream-json event (top-level OR nested sub-agent) seen while a
  // tool is in flight, AND (b) — as a schema-independent backstop — ANY stdout
  // chunk while a tool is in flight (see the child.stdout 'data' handler). A
  // legitimately long tool — a backend test run, a workflow/Task sub-agent
  // fan-out, or any future out-of-stream workflow that prints anything at all —
  // keeps this fresh by emitting SOMETHING, so the tool-use stall watchdog stays
  // quiet. A genuinely hung tool emits zero stdout, so this goes stale and the
  // stall watchdog fires. Reset to lastToolUseAt on each new top-level tool_use.
  // NOTE: the no-progress watchdog no longer consults this — it is suppressed
  // outright while toolInFlight is true (the in-flight case is owned solely by
  // the tool-use/workflow stall watchdog).
  let lastToolProgressAt: number | null = null;
  // Timestamp of the last model stream-json content event (assistant/result).
  // Drives the LLM-no-progress watchdog: a silent provider hang (429/529 the SDK
  // retries internally) emits non-content stdout but no content events, so this
  // goes stale while CLAUDE_STREAM_STALL_MS (raw stdout) keeps resetting.
  let lastStreamEventAt = Date.now();
  // Record that the in-flight tool produced genuine sub-progress. Cheap no-op
  // when no tool is in flight. IMPORTANT: a bare top-level `system` event is SDK
  // keepalive noise that a 429/529 provider-hang ALSO emits (the exact signal
  // the no-progress watchdog must still catch), so callers pass isProgress=false
  // for those. Real work — a nested sub-agent event from a workflow/Task fan-out,
  // or a top-level content/tool event — passes isProgress=true.
  const noteToolProgress = (isProgress: boolean) => {
    if (isProgress && toolInFlight) lastToolProgressAt = Date.now();
  };
  let lastCheckOutputOffset = -1;
  let codexTurnCompleted = false;

  sendLog(ws, `\n--- ${runner.displayName} starting ---\nPrompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}\nMode: ${LLM_EXECUTION_MODE}\n\n`);
  log(`Spawning ${runner.displayName} CLI...`);

  try {
    if (fs.existsSync(WORKSPACE)) {
      const noteScript = `#!/bin/bash
# Agent annotation tool — leave comments, issues, or vision suggestions
# Usage:
#   ./note comment "This module uses deprecated API" --page systems/sys-auth
#   ./note issue "Tests fail on large repos" --severity high
#   ./note vision "Consider adding agent reliability metrics"

TYPE="\${1:?Usage: note comment|issue|vision <content> [--page <key>] [--severity low|medium|high]}"
shift

CONTENT=""
PAGE=""
SEVERITY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --page) PAGE="$2"; shift 2 ;;
    --severity) SEVERITY="$2"; shift 2 ;;
    *) CONTENT="\${CONTENT:+$CONTENT }$1"; shift ;;
  esac
done

if [ -z "$CONTENT" ]; then
  echo "Error: content is required" >&2; exit 1
fi

API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
TOKEN="\${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"
TASK="\${TASK_ID:?TASK_ID not set}"
CURL_OPTS="-f -sS --max-time 15 --connect-timeout 10"

# Build JSON with printf to avoid shell expansion issues
JSON="{\\"task_id\\":\\"$TASK\\",\\"annotation_type\\":\\"$TYPE\\",\\"content\\":\\"$(echo "$CONTENT" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')\\""
[ -n "$PAGE" ] && JSON="\${JSON},\\"target_page\\":\\"$PAGE\\""
[ -n "$SEVERITY" ] && JSON="\${JSON},\\"severity\\":\\"$SEVERITY\\""
JSON="\${JSON}}"

printf '%s' "$JSON" | \\
  curl $CURL_OPTS -X POST "$API/api/agent/annotate" \\
    -H "Content-Type: application/json" \\
    -H "X-Agent-Token: $TOKEN" \\
    --data-binary @-
echo ""
`;
      fs.writeFileSync(`${WORKSPACE}/note`, noteScript, { mode: 0o755 });
      try {
        const excludePath = `${WORKSPACE}/.git/info/exclude`;
        const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
        if (!existing.includes('/note')) {
          fs.appendFileSync(excludePath, '/note\n');
        }
      } catch { /* shallow clone may lack .git/info */ }
    }
  } catch { /* workspace may not exist yet for some handlers */ }

  const exitCode = await new Promise<number>((resolve) => {
    let promptFile = '';
    let promptFd: number | undefined;
    let child = null as ReturnType<typeof spawn> | null;

    if (runCommand.promptInput === 'stdin-file') {
      promptFile = `/tmp/agent_prompt_${process.env.TASK_ID || 'unknown'}.txt`;
      writePrivateFileSync(promptFile, withExecutionModePrompt(prompt));
      promptFd = fs.openSync(promptFile, 'r');
    }

    child = spawn(runCommand.command, runCommand.args, {
      cwd: WORKSPACE,
      env: runCommand.env,
      detached: true,
      stdio: [runCommand.promptInput === 'stdin' ? 'pipe' : (promptFd ?? 'ignore'), 'pipe', 'pipe'],
    });
    recordLlmCallStart();
    if (typeof child.pid === 'number') {
      registerChildPid(child.pid);
    }
    if (promptFd !== undefined) fs.closeSync(promptFd);
    if (runCommand.promptInput === 'stdin') {
      try {
        child.stdin?.end(withExecutionModePrompt(prompt));
      } catch { /* child may exit immediately */ }
    }

    const timeoutMs = runCommand.timeoutMs;
    const timeoutMinutes = timeoutMs / 60000;
    let forceKillTimer: NodeJS.Timeout | null = null;
    let resultGraceTimer: NodeJS.Timeout | null = null;
    let stallWatchdog: NodeJS.Timeout | null = null;
    const originalPid = child.pid;
    const processGroupStillOwned = () =>
      originalPid !== undefined
      && child.pid === originalPid
      && child.exitCode === null
      && child.signalCode === null;
    const resetStallWatchdog = () => {
      if (stallWatchdog) clearTimeout(stallWatchdog);
      stallWatchdog = setTimeout(() => {
        if (!processGroupStillOwned()) return;
        sendLog(ws, `\n[runtime] stream-stall: no stdout for >${CLAUDE_STREAM_STALL_MS / 1000}s — sending SIGTERM so wrapper can proceed to commit/push.\n`);
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }, CLAUDE_STREAM_STALL_MS);
    };
    resetStallWatchdog();
    const killTimer = setTimeout(() => {
      log(`${runCommand.displayName} process timed out after ${timeoutMinutes} minutes — killing process group`);
      sendLog(ws, `\n[TIMEOUT] ${runCommand.displayName} exceeded ${timeoutMinutes}-minute time limit\n`);
      flags.timedOut = true;
      const pid = originalPid;
      if (pid !== undefined && processGroupStillOwned()) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
        forceKillTimer = setTimeout(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
        }, PROCESS_KILL_GRACE_MS);
      }
    }, timeoutMs);

    // Tool-use stall watchdog. Fires only when a tool has been in flight AND has
    // produced NO sub-progress (no stream-json event of any kind under it) for
    // longer than its silence budget. Keying off lastToolProgressAt (refreshed
    // by every event, including nested sub-agent events) rather than
    // lastToolUseAt (set once at tool start) means a legitimately long tool that
    // keeps streaming — a backend test run, a workflow/Task sub-agent fan-out —
    // is never false-killed; only a tool that goes truly silent times out.
    // Workflow/fan-out tools get the larger WORKFLOW_TOOL_STALL_TIMEOUT_MS budget.
    let stallWatchdogTimer: NodeJS.Timeout | null = setInterval(() => {
      const stallBudget = workflowToolInFlight
        ? WORKFLOW_TOOL_STALL_TIMEOUT_MS
        : TOOL_USE_STALL_TIMEOUT_MS;
      const lastActivity = lastToolProgressAt ?? lastToolUseAt;
      if (
        toolInFlight &&
        lastActivity !== null &&
        Date.now() - lastActivity > stallBudget &&
        processGroupStillOwned()
      ) {
        sendLog(ws, `\n[runtime] tool_use stall: ${workflowToolInFlight ? 'workflow ' : ''}tool in-flight with no sub-progress for >${stallBudget / 60000} min — sending SIGTERM\n`);
        flags.stalledMidStream = true;
        if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }
    }, 60_000);

    // LLM-no-progress watchdog: a silent provider hang (Anthropic 429/529 the
    // Claude Code SDK absorbs via internal retry) emits non-content stdout — so
    // the raw-stdout stall watchdog keeps resetting — while producing no
    // assistant/result content events and 0 output tokens. Detect the missing
    // content-event progress (when no tool is legitimately in flight) and report
    // it as a rate-limit so the orchestrator holds/reroutes instead of letting
    // the call ride to the 45-min wall-clock timeout → AGENT_TIMEOUT → retry-
    // budget burn → terminal failure of a healthy task (incident 2026-05-29).
    let llmProgressWatchdog: NodeJS.Timeout | null = setInterval(() => {
      // Fire ONLY when the content stream is stale AND no tool is in flight.
      //
      // The no-progress watchdog exists for ONE failure mode: a silent provider
      // hang (429/529 the SDK retries internally) WHILE the SDK is mid-
      // completion-request — which by definition happens with NO tool in flight.
      // A tool in flight — especially a workflow/Task sub-agent fan-out — is the
      // legitimate-long-wait case, and the parent's top-level stream is supposed
      // to go quiet while the model is parked waiting on that tool's result.
      //
      // The earlier C7 dual-staleness gate ALSO required `lastToolProgressAt`
      // (refreshed only on a recognized nested `parent_tool_use_id` sub-agent
      // event) to be fresh. The live confirmation came back NEGATIVE: during a
      // Task-tool wait the parent claude-code stdout does NOT surface nested
      // sub-agent events the parser recognizes as progress — it surfaces nothing
      // on the JSON channel or only bare top-level `system` keepalives (which we
      // deliberately ignore). So lastToolProgressAt froze at tool-launch time,
      // aged past LLM_NO_PROGRESS_STALL_MS, and this watchdog SIGTERMed a
      // perfectly healthy reviewer → false no_progress_timeout (the ~15-min
      // thrash loop #6979/#7031 chased). The Node wrapper already holds the
      // authoritative bit — `toolInFlight` (set when a top-level tool_use is
      // seen, cleared on its top-level tool-result `user`) — so we suppress the
      // no-progress kill while it is true and hand the in-flight case entirely
      // to the longer-budget tool-use/workflow stall watchdog above (keyed off
      // lastToolProgressAt ?? lastToolUseAt, so a workflow that streams ANYTHING
      // — including any raw stdout chunk, see the stdout 'data' handler — stays
      // alive, while one that goes fully dark is killed at 30/45 min as a
      // correctly-classified ClaudeTimeoutError, never a phantom rate_limit).
      //
      // A genuinely hung instance is still caught: (1) the raw-stdout stall
      // watchdog SIGTERMs after CLAUDE_STREAM_STALL_MS of ZERO bytes regardless
      // of tool state; (2) the tool-use/workflow stall watchdog owns the
      // in-flight case; (3) the wall-clock killTimer is the final backstop. So
      // disabling this kill while toolInFlight cannot mask a real hang.
      const contentStale = Date.now() - lastStreamEventAt > LLM_NO_PROGRESS_STALL_MS;
      if (
        !toolInFlight &&
        contentStale &&
        processGroupStillOwned()
      ) {
        sendLog(ws, `\n[runtime] llm-no-progress: no model content event for >${LLM_NO_PROGRESS_STALL_MS / 60000} min with no tool in flight — likely a provider hang (429/529 absorbed by SDK retry). Sending SIGTERM; reporting rate_limited.\n`);
        flags.llmNoProgressHang = true;
        if (llmProgressWatchdog) { clearInterval(llmProgressWatchdog); llmProgressWatchdog = null; }
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }
    }, 60_000);

    let checkRunawayWatchdog: NodeJS.Timeout | null = null;
    if (enableCheckWatchdog && !process.env.DISABLE_CHECK_RUNAWAY_WATCHDOG) {
      checkRunawayWatchdog = setInterval(() => {
        if (!processGroupStillOwned()) {
          if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
          return;
        }
        const { totalChecks, shouldTrigger } = getCheckRunawayStatus(
          CHECK_RUNAWAY_MIN_INVOCATIONS, CHECK_RUNAWAY_CONSECUTIVE_FAILURES,
        );
        let triggerReason: string | null = null;
        if (totalChecks >= MAX_CLAUDE_CHECK_INVOCATIONS) {
          triggerReason = `hard ceiling: ${totalChecks}/${MAX_CLAUDE_CHECK_INVOCATIONS} ./check calls in one session`;
        } else if (shouldTrigger) {
          triggerReason = `consecutive ./check failures after ${totalChecks} calls`;
        }
        if (triggerReason === null) return;
        sendLog(ws, `\n[runtime] check-runaway watchdog: ${triggerReason} — sending SIGTERM\n`);
        if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          forceKillTimer = setTimeout(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          }, PROCESS_KILL_GRACE_MS);
        }
      }, 60_000);
    }

    if (!child.stdout || !child.stderr) {
      throw new Error('spawn returned no stdio streams');
    }

    const pendingJsonChunks: string[] = [];
    // Awaitable drain mutex (cluster C7). The drain runs as a single
    // serialized async loop; a second caller must be able to AWAIT the
    // in-flight run rather than early-returning (the old boolean-only mutex
    // resolved the process promise while a drain was still mid-flight, so the
    // final unprocessed chunks — and their telemetry/result text — were
    // silently dropped at process exit). activeJsonDrain holds the in-flight
    // promise; drainJsonChunks() returns it so close/error can await completion.
    let activeJsonDrain: Promise<void> | null = null;
    const HEAVY_CHUNK_BYTES = 256 * 1024;
    const runJsonDrain = async () => {
      try {
        while (pendingJsonChunks.length > 0) {
          const chunk = pendingJsonChunks.shift();
          if (chunk === undefined) break;
          if (chunk.length >= HEAVY_CHUNK_BYTES) {
            await new Promise(r => setImmediate(r));
          }
          let linesSinceYield = 0;
          let lastYieldAt = Date.now();
          for (const line of chunk.split('\n').filter(Boolean)) {
            if (linesSinceYield >= WITHIN_CHUNK_YIELD_LINES
                || Date.now() - lastYieldAt >= WITHIN_CHUNK_YIELD_MS) {
              await new Promise(r => setImmediate(r));
              linesSinceYield = 0;
              lastYieldAt = Date.now();
            }
            if (!line.startsWith('{')) {
              sendLog(ws, line + '\n');
              continue;
            }
            let obj: ClaudeOutput | null = null;
            try {
              obj = JSON.parse(line) as ClaudeOutput;
            } catch {
              sendLog(ws, line + '\n');
              continue;
            }
            linesSinceYield += 1;
            const objType = obj.type || '';
            // A nested event is one emitted by a sub-agent the parent spawned via
            // the Task/Agent/Workflow tool. It is NOT a top-level model turn and
            // its tool-result `user` event must NOT clear the parent's toolInFlight.
            const isNested = obj.parent_tool_use_id != null && obj.parent_tool_use_id !== '';
            // Genuine sub-progress for the in-flight tool = a NESTED sub-agent
            // event (a workflow/Task fan-out actively streaming its sub-agents'
            // work). Top-level events are handled by the two orthogonal clocks
            // below: content events refresh lastStreamEventAt (no-progress
            // watchdog), a fresh top-level tool_use resets lastToolProgressAt
            // explicitly. Bare `system` keepalives count as neither — a 429/529
            // provider hang emits those too, so counting them would mask the very
            // hang the no-progress watchdog must catch.
            noteToolProgress(isNested);
            if (objType === 'stream_event') {
              // Incremental partial-message delta from --include-partial-messages
              // (content_block_delta text / thinking_delta). This is the
              // instance-level "model is actively generating/thinking RIGHT NOW"
              // signal: a slow-but-streaming opus turn (deep research/review on
              // 1M-context + extended thinking) emits these continuously, so the
              // no-progress watchdog stays fresh and the call rides to completion
              // instead of being false-flagged as a 429 hang. A genuinely hung
              // call (a real 429/529 the SDK retries silently) emits ZERO deltas,
              // so lastStreamEventAt still goes stale and the watchdog fires
              // correctly. Only top-level deltas count as model content progress;
              // nested deltas (a sub-agent under a workflow tool) already counted
              // as tool sub-progress via noteToolProgress(isNested) above and must
              // not mask a genuine top-level provider hang. Act minimally (just the
              // timer reset) and do NOT log every delta — --include-partial-messages
              // multiplies event volume and per-delta logging would flood the WS.
              if (!isNested) lastStreamEventAt = Date.now();
              // Any delta (top-level OR nested) is genuine in-flight activity, so
              // also count it as tool sub-progress when a tool is in flight —
              // consistent with the lastToolProgressAt logic (#6979). noteToolProgress
              // is a cheap no-op when no tool is in flight.
              noteToolProgress(true);
            } else if (objType === 'assistant') {
              // Only top-level assistant events count as model "content progress"
              // (the no-progress watchdog's content signal). Nested sub-agent
              // assistant events still count as tool sub-progress (noteToolProgress
              // above) but must not mask a genuine top-level provider hang.
              if (!isNested) lastStreamEventAt = Date.now();
              if (obj.message?.content) {
                for (const block of obj.message.content) {
                  if (block.type === 'text') {
                    sendLog(ws, (block.text ?? '') + '\n');
                    statusRef.value = (block.text ?? '').slice(0, 100);
                  } else if (block.type === 'tool_use') {
                    sendLog(ws, `[tool: ${strVal(block.name)}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                    statusRef.value = `Using tool: ${strVal(block.name)}`;
                    recordToolUse(strVal(block.name), block.input);
                    // A nested tool_use belongs to a sub-agent under an already
                    // in-flight workflow tool — it refreshes progress (above) but
                    // does not replace the parent's in-flight tracking.
                    if (!isNested) {
                      toolInFlight = true;
                      workflowToolInFlight = isWorkflowToolName(strVal(block.name));
                      lastToolUseAt = Date.now();
                      lastToolProgressAt = Date.now();
                    }
                    if (enableCheckWatchdog && strVal(block.name) === 'Bash') {
                      const inputPreview = JSON.stringify(block.input ?? {}).slice(0, 200);
                      if (isCheckInvocation(inputPreview)) {
                        lastCheckOutputOffset = allOutput.length;
                      }
                    }
                  }
                }
              }
            } else if (objType === 'result') {
              // Only a top-level result is the model's terminal turn. A nested
              // result (a sub-agent finishing under a workflow tool) is just more
              // sub-progress and must not arm the result-grace exit timer.
              if (!isNested) lastStreamEventAt = Date.now();  // terminal model event — reset no-progress watchdog
              lastResultText = (obj as { result?: string }).result ?? '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
              const usage = (obj as { result?: string; usage?: Record<string, unknown> }).usage;
              if (usage && typeof usage === 'object') {
                const inputTokens = Number(usage.input_tokens ?? 0) + Number(usage.cache_creation_input_tokens ?? 0) + Number(usage.cache_read_input_tokens ?? 0);
                const outputTokens = Number(usage.output_tokens ?? 0);
                recordLlmTokens(inputTokens, outputTokens);
              }
              if (!isNested && resultGraceTimer === null && processGroupStillOwned()) {
                resultGraceTimer = setTimeout(() => {
                  if (!processGroupStillOwned()) return;
                  sendLog(
                    ws,
                    `\n[runtime] result event received >${CLAUDE_RESULT_GRACE_MS / 1000}s ago without process exit — `
                      + `sending SIGTERM so wrapper can proceed to commit/push.\n`,
                  );
                  const pid = originalPid;
                  if (pid !== undefined) {
                    try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
                    forceKillTimer = setTimeout(() => {
                      if (!processGroupStillOwned()) return;
                      try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
                    }, PROCESS_KILL_GRACE_MS);
                  }
                }, CLAUDE_RESULT_GRACE_MS);
              }
            } else if (objType === 'error') {
              const errLine = `[claude-stream error] ${(obj as { error?: { message?: string } }).error?.message ?? ''}\n`;
              sendLog(ws, errLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, errLine, STDERR_TAIL_BYTES);
            } else if (objType === 'system' || objType === 'user') {
              // A nested `user` event is a sub-agent's tool-result under a still-
              // running workflow tool; it must NOT clear the parent's toolInFlight
              // (doing so would re-expose the parent to a false no-progress fire
              // while the workflow keeps running). Only a top-level tool-result
              // closes the parent's in-flight tool.
              if (objType === 'user' && !isNested) {
                toolInFlight = false;
                workflowToolInFlight = false;
                lastToolProgressAt = null;
                if (enableCheckWatchdog && lastCheckOutputOffset >= 0) {
                  const slice = allOutput.slice(lastCheckOutputOffset);
                  recordCheckOutcome(!/\[check\].*FAILED/.test(slice));
                  lastCheckOutputOffset = -1;
                }
              }
            } else {
              sendLog(ws, `[claude-stream unknown type: ${objType}]\n`);
            }
          }
          await new Promise(r => setImmediate(r));
        }
      } finally {
        activeJsonDrain = null;
      }
    };
    // Returns the in-flight drain promise so callers (close/error final drain)
    // can await it. Starts a new drain only if none is running; a running drain
    // already loops until pendingJsonChunks is empty, so pushing more chunks and
    // awaiting the same promise is safe.
    const drainJsonChunks = (): Promise<void> => {
      if (!activeJsonDrain) activeJsonDrain = runJsonDrain();
      return activeJsonDrain;
    };

    const pendingCodexChunks: string[] = [];
    let activeCodexDrain: Promise<void> | null = null;
    let codexCarry = '';
    const _codexToolNameForItem = (item: unknown): string | null => {
      if (!item || typeof item !== 'object') return null;
      const itemType = (item as { type?: string }).type;
      switch (itemType) {
        case 'command_execution':
          return 'Bash';
        case 'file_change':
          return 'apply_patch';
        case 'mcp_tool_call': {
          const t = (item as { tool?: string }).tool;
          return typeof t === 'string' && t ? `MCP:${t}` : 'MCP';
        }
        case 'web_search':
          return 'WebSearch';
        case 'todo_list':
          return 'TodoWrite';
        default:
          return null;
      }
    };
    const _codexItemPreview = (item: unknown): string => {
      if (!item || typeof item !== 'object') return '';
      const it = item as Record<string, unknown>;
      switch (it.type) {
        case 'command_execution':
          return typeof it.command === 'string' ? it.command : '';
        case 'file_change': {
          const changes = Array.isArray(it.changes) ? it.changes : [];
          return `${changes.length} file change(s)`;
        }
        case 'mcp_tool_call':
          return JSON.stringify(it.arguments ?? {}).slice(0, 200);
        case 'web_search':
          return typeof it.query === 'string' ? it.query : '';
        case 'agent_message':
        case 'reasoning':
          return typeof it.text === 'string' ? it.text.slice(0, 200) : '';
        default:
          return '';
      }
    };
    const runCodexDrain = async () => {
      try {
        while (pendingCodexChunks.length > 0) {
          const chunk = pendingCodexChunks.shift();
          if (chunk === undefined) break;
          if (chunk.length >= 256 * 1024) {
            await new Promise(r => setImmediate(r));
          }
          const combined = codexCarry + chunk;
          const lastNewline = combined.lastIndexOf('\n');
          const completeChunk = lastNewline >= 0 ? combined.slice(0, lastNewline) : '';
          codexCarry = lastNewline >= 0 ? combined.slice(lastNewline + 1) : combined;
          let linesSinceYield = 0;
          let lastYieldAt = Date.now();
          for (const line of completeChunk.split('\n')) {
            if (!line) continue;
            if (linesSinceYield >= WITHIN_CHUNK_YIELD_LINES
                || Date.now() - lastYieldAt >= WITHIN_CHUNK_YIELD_MS) {
              await new Promise(r => setImmediate(r));
              linesSinceYield = 0;
              lastYieldAt = Date.now();
            }
            if (!line.startsWith('{')) {
              sendLog(ws, line + '\n');
              continue;
            }
            let obj: Record<string, unknown> | null = null;
            try {
              obj = JSON.parse(line) as Record<string, unknown>;
            } catch {
              sendLog(ws, line + '\n');
              continue;
            }
            linesSinceYield += 1;
            const eventType = typeof obj.type === 'string' ? obj.type : '';
            // Codex items are one-shot (started → completed in sequence) with no
            // nested sub-agent stream, so tool sub-progress is tracked explicitly
            // at item.started (lastToolProgressAt set there); there is no separate
            // sub-progress signal to fold in here.
            if (eventType === 'thread.started') {
              const tid = typeof obj.thread_id === 'string' ? obj.thread_id : '';
              sendLog(ws, `[codex] thread.started ${tid}\n`);
            } else if (eventType === 'turn.started') {
              lastStreamEventAt = Date.now();  // model producing — reset no-progress watchdog
              sendLog(ws, '[codex] turn.started\n');
              statusRef.value = 'Codex thinking';
            } else if (eventType === 'turn.completed') {
              lastStreamEventAt = Date.now();  // model producing — reset no-progress watchdog
              sendLog(ws, '[codex] turn.completed\n');
              codexTurnCompleted = true;
              const usage = obj.usage as Record<string, unknown> | undefined;
              if (usage && typeof usage === 'object') {
                const inputTokens = Number(usage.input_tokens ?? 0) + Number(usage.cache_creation_input_tokens ?? 0) + Number(usage.cache_read_input_tokens ?? 0);
                const outputTokens = Number(usage.output_tokens ?? 0);
                recordLlmTokens(inputTokens, outputTokens);
              }
            } else if (eventType === 'turn.failed') {
              const err = (obj.error as { message?: string } | undefined)?.message ?? '';
              const oneLine = `[codex] turn.failed: ${err}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
            } else if (eventType === 'error') {
              const msg = typeof obj.message === 'string' ? obj.message : '';
              const oneLine = `[codex] error: ${msg}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
            } else if (
              eventType === 'item.started'
              || eventType === 'item.updated'
              || eventType === 'item.completed'
            ) {
              lastStreamEventAt = Date.now();  // model producing — reset no-progress watchdog
              const item = obj.item as Record<string, unknown> | undefined;
              const itemType = item && typeof item.type === 'string' ? item.type : '';
              const preview = _codexItemPreview(item);
              const toolName = _codexToolNameForItem(item);
              if (toolName) {
                if (eventType === 'item.started' || eventType === 'item.updated') {
                  sendLog(ws, `[tool: ${toolName}] ${preview.slice(0, 200)}\n`);
                  statusRef.value = `Using tool: ${toolName}`;
                  recordToolUse(toolName, item ?? {});
                  toolInFlight = true;
                  workflowToolInFlight = isWorkflowToolName(toolName);
                  lastToolUseAt = Date.now();
                  lastToolProgressAt = Date.now();
                  if (
                    enableCheckWatchdog
                    && toolName === 'Bash'
                    && isCheckInvocation(preview)
                  ) {
                    lastCheckOutputOffset = allOutput.length;
                  }
                } else {
                  toolInFlight = false;
                  workflowToolInFlight = false;
                  lastToolProgressAt = null;
                  const status = item && typeof item.status === 'string' ? item.status : '';
                  sendLog(ws, `[tool: ${toolName}] ${status}\n`);
                }
              } else if (itemType === 'agent_message') {
                if (eventType === 'item.completed' || eventType === 'item.updated') {
                  sendLog(ws, preview + '\n');
                  if (preview) statusRef.value = preview.slice(0, 100);
                  if (eventType === 'item.completed') {
                    lastResultText = (item != null && typeof item.text === 'string') ? item.text : preview;
                  }
                }
              } else if (itemType === 'reasoning') {
                if (eventType === 'item.completed') {
                  statusRef.value = `Reasoning: ${preview.slice(0, 80)}`;
                }
              } else if (itemType === 'error') {
                const msg = item && typeof item.message === 'string' ? item.message : '';
                const oneLine = `[codex] item error: ${msg}\n`;
                sendLog(ws, oneLine);
                stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
              }
            }
          }
          await new Promise(r => setImmediate(r));
        }
      } finally {
        activeCodexDrain = null;
      }
    };
    const drainCodexChunks = (): Promise<void> => {
      if (!activeCodexDrain) activeCodexDrain = runCodexDrain();
      return activeCodexDrain;
    };

    child.stdout.on('data', (data: Buffer) => {
      resetStallWatchdog();
      // Coarse wrapper-level liveness for the in-flight tool, independent of the
      // stream-json event schema. The tool-use/workflow stall watchdog keys off
      // lastToolProgressAt, which the JSON drain only refreshes on a recognized
      // nested `parent_tool_use_id` sub-agent event. A Task-tool wait does not
      // reliably surface those (live confirmation was negative), and any future
      // out-of-stream workflow (a review backgrounded via Bash, a cloud-review
      // poll) may print only plain text. So: while a tool is in flight, treat
      // ANY stdout chunk — JSON sub-progress, a subagent log line, even non-JSON
      // noise — as proof the in-flight tool is alive and refresh its clock. The
      // in-flight tool is then bounded purely by going FULLY dark (no stdout at
      // all) for its silence budget, which is exactly the genuinely-hung
      // signature. The no-progress watchdog is unaffected (it is suppressed
      // while toolInFlight) and a fresh top-level content event still resets
      // lastStreamEventAt for it in the JSON drain.
      if (toolInFlight) lastToolProgressAt = Date.now();
      const text = data.toString();
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      if (runCommand.outputMode === 'claude-stream-json') {
        pendingJsonChunks.push(text);
        drainJsonChunks().catch((e: unknown) => {
          sendLog(ws, `[runtime] drainJsonChunks error: ${String(e)}\n`);
        });
      } else if (runCommand.outputMode === 'codex-jsonl') {
        pendingCodexChunks.push(text);
        drainCodexChunks().catch((e: unknown) => {
          sendLog(ws, `[runtime] drainCodexChunks error: ${String(e)}\n`);
        });
      } else {
        sendLog(ws, text);
        const trimmed = text.trim();
        if (trimmed) {
          lastResultText = `${lastResultText}${text}`.trim();
          statusRef.value = trimmed.slice(0, 100);
        }
      }
    });

    child.stderr.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += text;
      stderrBuffer = appendBoundedTail(stderrBuffer, text, STDERR_TAIL_BYTES);
      sendLog(ws, `[stderr] ${text}`);
    });

    const drainPendingThenResolve = async (resolveValue: number) => {
      try {
        // Await the in-flight drain (if any) AND any leftover queued chunks.
        // The old boolean mutex made drainJsonChunks() early-return when a
        // drain was already active, so resolve() fired before the in-flight
        // run finished — unprocessed chunks (and their result text / token
        // telemetry) were silently lost at process exit. Now drainJsonChunks()
        // returns the in-flight promise; awaiting it guarantees completion.
        // Loop to catch chunks that the data handler pushed while we awaited
        // (a fresh drain wouldn't be re-armed once activeJsonDrain cleared).
        while (activeJsonDrain || pendingJsonChunks.length > 0) {
          await drainJsonChunks();
        }
        while (activeCodexDrain || pendingCodexChunks.length > 0) {
          await drainCodexChunks();
        }
      } catch (e) {
        sendLog(ws, `[runtime] final drain error: ${String(e)}\n`);
      }
      resolve(resolveValue);
    };
    child.on('error', (err) => {
      clearTimeout(killTimer);
      if (forceKillTimer) clearTimeout(forceKillTimer);
      if (resultGraceTimer) clearTimeout(resultGraceTimer);
      if (stallWatchdog) clearTimeout(stallWatchdog);
      if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
      if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
      if (llmProgressWatchdog) { clearInterval(llmProgressWatchdog); llmProgressWatchdog = null; }
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      sendLog(ws, `[error] ${err.message}\n`);
      void drainPendingThenResolve(1);
    });
    child.on('close', (code, signal) => {
      const closeSignal = signal ?? null;
      clearTimeout(killTimer);
      if (forceKillTimer) clearTimeout(forceKillTimer);
      if (resultGraceTimer) clearTimeout(resultGraceTimer);
      if (stallWatchdog) clearTimeout(stallWatchdog);
      if (stallWatchdogTimer) { clearInterval(stallWatchdogTimer); stallWatchdogTimer = null; }
      if (checkRunawayWatchdog) { clearInterval(checkRunawayWatchdog); checkRunawayWatchdog = null; }
      if (llmProgressWatchdog) { clearInterval(llmProgressWatchdog); llmProgressWatchdog = null; }
      if (typeof originalPid === 'number') {
        unregisterChildPid(originalPid);
      }
      if (closeSignal === 'SIGKILL' && !flags.timedOut) {
        flags.providerKilledByOOM = true;
        const oomLine = `EOOM: ${runCommand.displayName} process received external SIGKILL before agent timeout; likely cgroup OOM\n`;
        stderrBuffer = appendBoundedTail(stderrBuffer, oomLine, STDERR_TAIL_BYTES);
        if (allOutput.length < MAX_OUTPUT_BYTES) allOutput += oomLine;
        sendLog(ws, `[stderr] ${oomLine}`);
      }
      if (promptFile) {
        try { fs.unlinkSync(promptFile); } catch { /* ignore */ }
      }
      log(`${runCommand.displayName} exited with code ${code ?? 'null'}${closeSignal ? ` signal ${closeSignal}` : ''}`);
      void drainPendingThenResolve(code ?? 1);
    });
  });

  if (runCommand.outputMode === 'text') {
    recordLlmTokens(
      estimateTokensFromChars(prompt.length),
      estimateTokensFromChars(lastResultText.length),
    );
  }

  sendLog(ws, `\n--- ${runner.displayName} exited (code: ${exitCode}) ---\n`);

  if (flags.timedOut) {
    throw new ClaudeTimeoutError(`${runner.displayName} exceeded ${runCommand.timeoutMs / 60000}-minute time limit`);
  }
  if (flags.stalledMidStream) {
    throw new ClaudeTimeoutError(`${runner.displayName} mid-stream tool-use stall: tool in-flight for >${TOOL_USE_STALL_TIMEOUT_MS / 60000} min without result`);
  }
  // Computed up here (before the no-progress branch) so the no-progress watchdog
  // can consult it: only a CONFIRMED rate-limit message blocks the account.
  const combined = stderrBuffer + allOutput.slice(-LOG_TAIL_BYTES);
  const sawConfirmedRateLimit = matchesRateLimit(combined, runner.provider, runner.authMode);

  // Checked before providerKilledByOOM: the watchdog's own SIGTERM→SIGKILL can
  // trip the SIGKILL-without-timeout OOM heuristic above, so this branch must win.
  // The no-progress watchdog now fires ONLY on a genuinely silent call (with
  // --include-partial-messages, a slow-but-streaming opus turn keeps emitting
  // deltas and never trips it). Distinguish two cases:
  //   • CONFIRMED rate-limit text present → RateLimitError → failureCategory
  //     'rate_limited' → orchestrator blocks the account's model + applies the
  //     canonical cooldown (UNCHANGED — real rate-limit handling preserved).
  //   • NO confirmed rate-limit text → a transient provider hang of unknown cause.
  //     The pipeline runs 6 Max-20x accounts and is NOT actually rate-limited, so
  //     blocking the healthy account here is a self-inflicted cascade. Report a
  //     transient 'no_progress_timeout' instead: the orchestrator requeues the
  //     task (without burning retry) and leaves the account AVAILABLE. The hang is
  //     still bounded (we SIGTERM'd + requeue), so it is never immortal.
  if (flags.llmNoProgressHang) {
    if (sawConfirmedRateLimit) {
      const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
      throw new RateLimitError(`${runner.displayName} produced no model output and hit a confirmed rate limit — ${snippet}`);
    }
    throw new NoProgressTimeoutError(`${runner.displayName} produced no model output (no stream deltas) for >${LLM_NO_PROGRESS_STALL_MS / 60000} min — transient provider hang, no confirmed rate-limit message`);
  }
  if (flags.providerKilledByOOM) {
    const err = new ClaudeTimeoutError(`EOOM: ${runner.displayName} received external SIGKILL before agent timeout; likely cgroup OOM`);
    (err as Error & { code?: string }).code = 'EOOM';
    throw err;
  }

  if (sawConfirmedRateLimit) {
    const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
    throw new RateLimitError(`${runner.displayName} API usage exhausted — ${snippet}`);
  }

  const stderrTail = stderrBuffer.slice(-500);
  return {
    exitCode,
    allOutput,
    lastResultText: lastResultText || allOutput.trim(),
    codexTurnCompleted,
    stderrTail,
  };
}
