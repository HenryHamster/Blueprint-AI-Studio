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

export const PROVIDER: ProviderName = normalizeProvider(process.env.PROVIDER);
export const AUTH_MODE: AuthMode = normalizeAuthMode(process.env.AUTH_MODE, PROVIDER);
export const LLM_EXECUTION_MODE = process.env.LLM_EXECUTION_MODE || 'default';

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

export const LLM_CLI_TRANSLATION: LLMCliTranslation | null = parseLLMCliTranslation();

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

interface ClaudeOutputBlock { type: string; text?: string; name?: string; id?: string; input?: unknown }
export interface ClaudeOutput {
  type: string;
  message?: { content: ClaudeOutputBlock[] };
  result?: string;
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
      const args = ['-p', '--output-format', 'stream-json', '--verbose', '--dangerously-skip-permissions'];
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
  // Timestamp of the last model stream-json content event (assistant/result).
  // Drives the LLM-no-progress watchdog: a silent provider hang (429/529 the SDK
  // retries internally) emits non-content stdout but no content events, so this
  // goes stale while CLAUDE_STREAM_STALL_MS (raw stdout) keeps resetting.
  let lastStreamEventAt = Date.now();
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

    let stallWatchdogTimer: NodeJS.Timeout | null = setInterval(() => {
      if (
        toolInFlight &&
        lastToolUseAt !== null &&
        Date.now() - lastToolUseAt > TOOL_USE_STALL_TIMEOUT_MS &&
        processGroupStillOwned()
      ) {
        sendLog(ws, `\n[runtime] tool_use stall: tool in-flight for >${TOOL_USE_STALL_TIMEOUT_MS / 60000} min without result — sending SIGTERM\n`);
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
      if (
        !toolInFlight &&
        Date.now() - lastStreamEventAt > LLM_NO_PROGRESS_STALL_MS &&
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
    let drainingJsonChunks = false;
    const HEAVY_CHUNK_BYTES = 256 * 1024;
    const drainJsonChunks = async () => {
      if (drainingJsonChunks) return;
      drainingJsonChunks = true;
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
            if (objType === 'assistant') {
              lastStreamEventAt = Date.now();  // model is producing — reset no-progress watchdog
              if (obj.message?.content) {
                for (const block of obj.message.content) {
                  if (block.type === 'text') {
                    sendLog(ws, (block.text ?? '') + '\n');
                    statusRef.value = (block.text ?? '').slice(0, 100);
                  } else if (block.type === 'tool_use') {
                    sendLog(ws, `[tool: ${strVal(block.name)}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                    statusRef.value = `Using tool: ${strVal(block.name)}`;
                    recordToolUse(strVal(block.name), block.input);
                    toolInFlight = true;
                    lastToolUseAt = Date.now();
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
              lastStreamEventAt = Date.now();  // terminal model event — reset no-progress watchdog
              lastResultText = (obj as { result?: string }).result ?? '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
              const usage = (obj as { result?: string; usage?: Record<string, unknown> }).usage;
              if (usage && typeof usage === 'object') {
                const inputTokens = Number(usage.input_tokens ?? 0) + Number(usage.cache_creation_input_tokens ?? 0) + Number(usage.cache_read_input_tokens ?? 0);
                const outputTokens = Number(usage.output_tokens ?? 0);
                recordLlmTokens(inputTokens, outputTokens);
              }
              if (resultGraceTimer === null && processGroupStillOwned()) {
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
              if (objType === 'user') {
                toolInFlight = false;
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
        drainingJsonChunks = false;
      }
    };

    const pendingCodexChunks: string[] = [];
    let drainingCodexChunks = false;
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
    const drainCodexChunks = async () => {
      if (drainingCodexChunks) return;
      drainingCodexChunks = true;
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
                  lastToolUseAt = Date.now();
                  if (
                    enableCheckWatchdog
                    && toolName === 'Bash'
                    && isCheckInvocation(preview)
                  ) {
                    lastCheckOutputOffset = allOutput.length;
                  }
                } else {
                  toolInFlight = false;
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
        drainingCodexChunks = false;
      }
    };

    child.stdout.on('data', (data: Buffer) => {
      resetStallWatchdog();
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
        if (pendingJsonChunks.length > 0 || drainingJsonChunks) {
          await drainJsonChunks();
        }
        if (pendingCodexChunks.length > 0 || drainingCodexChunks) {
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
  // Checked before providerKilledByOOM: the watchdog's own SIGTERM→SIGKILL can
  // trip the SIGKILL-without-timeout OOM heuristic above, so this RateLimitError
  // must win. RateLimitError → failureCategory 'rate_limited' → no retry-budget
  // burn; the orchestrator holds/reroutes instead of failing the task.
  if (flags.llmNoProgressHang) {
    throw new RateLimitError(`${runner.displayName} produced no model output for >${LLM_NO_PROGRESS_STALL_MS / 60000} min — provider hang/throttle (429/529 absorbed by SDK retry)`);
  }
  if (flags.providerKilledByOOM) {
    const err = new ClaudeTimeoutError(`EOOM: ${runner.displayName} received external SIGKILL before agent timeout; likely cgroup OOM`);
    (err as Error & { code?: string }).code = 'EOOM';
    throw err;
  }

  const combined = stderrBuffer + allOutput.slice(-LOG_TAIL_BYTES);
  if (matchesRateLimit(combined, runner.provider, runner.authMode)) {
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
