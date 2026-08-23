import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync, spawn, spawnSync } from 'child_process';
import type WebSocket from 'ws';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import { getCheckRunawayStatus, isCheckInvocation, recordCheckOutcome } from '../fix-loop-events.js';
import {
  CHECK_RUNAWAY_CONSECUTIVE_FAILURES, CHECK_RUNAWAY_MIN_INVOCATIONS, CLAUDE_RESULT_GRACE_MS,
  CLAUDE_STREAM_STALL_MS, LLM_NO_PROGRESS_STALL_MS, LOG_TAIL_BYTES,
  MAX_CLAUDE_CHECK_INVOCATIONS, MAX_OUTPUT_BYTES, OUTPUT_TAIL_AFTER_CAP_BYTES,
  PROCESS_KILL_GRACE_MS, STDERR_TAIL_BYTES, TOOL_USE_STALL_TIMEOUT_MS,
  WORKFLOW_TOOL_STALL_TIMEOUT_MS, isWorkflowToolName,
  WITHIN_CHUNK_YIELD_LINES, WITHIN_CHUNK_YIELD_MS, claudeTimeoutForEffort,
} from '../constants.js';
import {
  WORKSPACE, ClaudeTimeoutError, getAgentToken, log, recordLlmCallStart,
  recordLlmTokens, recordProviderReportedUsage, recordNumTurns, recordToolResultBytes, recordToolUse, sanitize, sendLog, strVal, estimateTokensFromChars,
} from '../index.js';
import {
  AGENT_HELPER_NAMES,
  writePlaybookHelper,
  writeMessagesHelper,
  writeSymbolsHelper,
  retryPostShellFn,
} from '../prompt.js';
import { AGENT_TOKEN_FILE_ENV, GITHUB_TOKEN_FILE_ENV, agentTokenShellFn, jsonStrShellFn } from '../token-file.js';
import { normalizeProviderTokenUsage, extractProviderReportedUsage } from './usage.js';
import { applyDeepSeekModelEnv, pinDeepSeekModel } from './deepseek-env.js';
import { maybeConsumeCodexWeeklyReset } from './codex-rate-limit-reset.js';

export type ProviderName = 'anthropic' | 'openai' | 'gemini' | 'zai' | 'deepseek';
export type AuthMode = 'oauth' | 'api_key' | 'service_account';
export type OutputMode = 'claude-stream-json' | 'codex-jsonl' | 'text';

const ZAI_ANTHROPIC_BASE_URL = 'https://api.z.ai/api/anthropic';
const DEEPSEEK_ANTHROPIC_BASE_URL = 'https://api.deepseek.com/anthropic';

/**
 * Thrown when a required provider runtime capability is unavailable (for
 * example a CLI binary is absent from PATH). The agent exits with code 42 so
 * the runner does NOT increment its failure streak — this is an infra decline,
 * not a real task failure.
 */
export class RuntimeUnavailableError extends Error {
  readonly failureCategory = 'runtime_unavailable' as const;
  readonly gracefulExitCode = 42 as const;
  constructor(cli: string) {
    super(`runtime_unavailable:${cli}`);
    this.name = 'RuntimeUnavailableError';
  }
}

/** Returns true if `command` resolves to an executable file on PATH. */
export function commandExistsOnPath(command: string): boolean {
  const pathEnv = process.env.PATH || '';
  const sep = process.platform === 'win32' ? ';' : ':';
  for (const dir of pathEnv.split(sep)) {
    if (!dir) continue;
    const fullPath = `${dir}/${command}`;
    try {
      fs.accessSync(fullPath, fs.constants.X_OK);
      return true;
    } catch { /* not found or not executable in this dir */ }
  }
  return false;
}

// Test override: allows unit tests to bypass the pre-flight CLI check without
// actually installing provider binaries. Set to null to re-enable the real check.
let _commandExistsOverride: ((command: string) => boolean) | null = null;
export function __setCommandExistsOverrideForSmoke(fn: ((command: string) => boolean) | null): void {
  _commandExistsOverride = fn;
}

function _commandExists(command: string): boolean {
  return _commandExistsOverride ? _commandExistsOverride(command) : commandExistsOnPath(command);
}

const CLAUDE_PARTIAL_MESSAGES_FLAG = '--include-partial-messages';
const CLAUDE_RESIDENT_REQUIRED_FLAGS = new Set([
  '--input-format',
  '--append-system-prompt',
  '--mcp-config',
  '--strict-mcp-config',
  '--allowedTools',
]);
const CLAUDE_OPTIONAL_FLAG_VALUE_COUNTS: Readonly<Record<string, number>> = {
  [CLAUDE_PARTIAL_MESSAGES_FLAG]: 0,
  '--effort': 1,
  '--safe-mode': 0,
  '--tools': 1,
  '--disable-slash-commands': 0,
  '--strict-mcp-config': 0,
  '--mcp-config': 1,
  '--allowedTools': 1,
  '--max-turns': 1,
  '--no-session-persistence': 0,
};
const claudeCapabilityCache = new Map<string, ReadonlySet<string> | null>();
const claudeCapabilityByEnvironment = new Map<string, ReadonlySet<string> | null>();

function claudeCapabilityEnvironmentKey(command: string, env: NodeJS.ProcessEnv): string {
  // The same literal command can resolve to different binaries across the
  // normal provider environment and isolated memory/foreground environments.
  // HOME/config can also select a packaged wrapper with a different help
  // surface. Never let an earlier probe poison a later Resident launch merely
  // because both commands happen to be named "claude".
  return [
    command,
    env.PATH ?? '',
    env.HOME ?? '',
    env.CLAUDE_CONFIG_DIR ?? '',
    env.CLAUDE_CODE_SIMPLE ?? '',
  ].join('\u0000');
}

export function claudeHelpSupportsPartialMessages(helpText: string): boolean {
  return helpText.includes(CLAUDE_PARTIAL_MESSAGES_FLAG);
}

function claudeFlagsFromHelp(helpText: string): ReadonlySet<string> {
  return new Set(helpText.match(/--[a-zA-Z0-9-]+/g) ?? []);
}

function claudeHelpOutput(command: string, env: NodeJS.ProcessEnv): string | null {
  const result = spawnSync(command, ['--help'], {
    env,
    encoding: 'utf8',
    timeout: 5_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) return null;
  // Claude distributions do not consistently choose the same stream for help.
  // The runner readiness probe already combines both; the launch-time probe
  // must do the same or a runner can be admitted and then rejected by Resident.
  const output = [result.stdout, result.stderr]
    .filter((value): value is string => typeof value === 'string' && value.length > 0)
    .join('\n')
    .trim();
  return output || null;
}

/**
 * Returns the optional Claude CLI capabilities advertised by this binary.
 * A failed or ambiguous probe deliberately returns null (fail open): launch
 * compatibility is then determined by the CLI's normal error path instead of
 * treating an unavailable `--help` surface as proof that flags are unsupported.
 */
export function probeClaudeCapabilities(command: string, env: NodeJS.ProcessEnv): ReadonlySet<string> | null {
  const environmentKey = claudeCapabilityEnvironmentKey(command, env);
  const environmentCached = claudeCapabilityByEnvironment.get(environmentKey);
  if (environmentCached !== undefined) return environmentCached;
  try {
    const version = execFileSync(command, ['--version'], {
      env, encoding: 'utf8', timeout: 5_000, stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
    if (!version) return null;
    const cacheKey = `${environmentKey}\u0000${version}`;
    const cached = claudeCapabilityCache.get(cacheKey);
    if (cached !== undefined) {
      claudeCapabilityByEnvironment.set(environmentKey, cached);
      return cached;
    }
    const helpText = claudeHelpOutput(command, env) ?? '';
    let capabilities = helpText ? claudeFlagsFromHelp(helpText) : null;
    // Some packaged Claude CLIs have intermittently emitted a truncated help
    // surface. Retry a suspiciously incomplete result before caching it; a
    // stable known-negative result remains known-negative and the Resident's
    // required-flag gate still fails closed.
    if (capabilities && [...CLAUDE_RESIDENT_REQUIRED_FLAGS].some((flag) => !capabilities?.has(flag))) {
      try {
        const retryText = claudeHelpOutput(command, env) ?? '';
        const retryCapabilities = retryText ? claudeFlagsFromHelp(retryText) : null;
        if (retryCapabilities) {
          // A truncated help render can omit a different section on each call.
          // Union independently observed flags instead of choosing one partial
          // render and discarding valid evidence from the other.
          capabilities = new Set([...capabilities, ...retryCapabilities]);
        }
      } catch {
        // Keep the first definite result; required consumers still fail closed.
      }
    }
    claudeCapabilityCache.set(cacheKey, capabilities);
    claudeCapabilityByEnvironment.set(environmentKey, capabilities);
    return capabilities;
  } catch {
    return null;
  }
}

export function stripUnsupportedClaudeOptionalArgs(
  args: readonly string[], capabilities: ReadonlySet<string> | null,
): string[] {
  if (!capabilities) return [...args];
  const compatible: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const valueCount = CLAUDE_OPTIONAL_FLAG_VALUE_COUNTS[args[index]];
    if (Object.hasOwn(CLAUDE_OPTIONAL_FLAG_VALUE_COUNTS, args[index]) && !capabilities.has(args[index])) {
      index += valueCount;
      continue;
    }
    compatible.push(args[index]);
  }
  return compatible;
}

export function __resetClaudeCapabilityCacheForTesting(): void {
  claudeCapabilityCache.clear();
  claudeCapabilityByEnvironment.clear();
}

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
      // Org billing cap: "You've hit your org's monthly spend limit · ask your
      // admin to raise it at claude.ai/settings/usage". Matched on the
      // apostrophe-free tail so a curly-quote "org's" can't break the match.
      'monthly spend limit',
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
      'monthly spend limit',
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
      'insufficient_quota',
      'usage_not_included',
      'you exceeded your current quota',
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
  zai: {
    api_key: [
      'rate limit exceeded',
      'rate_limit',
      'too many requests',
      'api error: rate limit',
      'api error: 429',
      'status 429',
      'quota exceeded',
      'insufficient balance',
      'insufficient_balance',
      'insufficient quota',
      'api usage exhausted',
    ],
  },
  deepseek: {
    api_key: [
      'rate limit exceeded',
      'rate_limit',
      'too many requests',
      'api error: rate limit',
      'api error: 429',
      'status 429',
      'quota exceeded',
      'insufficient balance',
      'insufficient_balance',
      'insufficient quota',
      'balance not enough',
      'api usage exhausted',
    ],
  },
} as const;

const API_KEY_CAPACITY_PATTERNS = [
  'overloaded',
  'resource exhausted',
  'concurrency limit',
  'provider capacity',
  'service capacity',
  'server capacity',
  'at capacity',
  'capacity exceeded',
  'temporarily unavailable',
  'service unavailable',
] as const;

const RATE_LIMIT_PROVIDER_CONTEXT_PATTERNS: Readonly<Record<ProviderName, readonly string[]>> = {
  anthropic: ['api.anthropic.com'],
  openai: ['api.openai.com', 'openai api error', 'error from openai'],
  gemini: ['generativelanguage.googleapis.com', 'aiplatform.googleapis.com', 'gemini'],
  zai: ['api.z.ai', 'z.ai api error', 'zai api error', 'error from z.ai', 'error from zai'],
  deepseek: ['api.deepseek.com', 'deepseek api error', 'error from deepseek'],
} as const;

function normalizeProvider(raw: string | undefined): ProviderName {
  if (raw === 'openai' || raw === 'gemini' || raw === 'zai' || raw === 'deepseek') return raw;
  return 'anthropic';
}

function normalizeAuthMode(raw: string | undefined, provider: ProviderName): AuthMode {
  if (provider === 'zai' || provider === 'deepseek') return 'api_key';
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

/**
 * Whether the active provider exposes a native sub-agent / Task fan-out
 * primitive that lets a single orchestrating pass run several independent
 * review passes in parallel. Claude Code-backed providers expose the
 * Task/Agent tool; codex and gemini have no equivalent, so callers that depend
 * on native fan-out must fall back to a serial
 * path for those providers. This is the single source of truth for "is the
 * native fan-out primitive available" — detect availability, never assume.
 */
export function providerSupportsNativeSubagents(provider: ProviderName = PROVIDER): boolean {
  return provider === 'anthropic' || provider === 'zai' || provider === 'deepseek';
}

export function providerAutoInjectsClaudeMd(provider: ProviderName = PROVIDER): boolean {
  return provider === 'anthropic';
}

function getRateLimitPatterns(provider: ProviderName, authMode: AuthMode): readonly string[] {
  return RATE_LIMIT_PATTERNS[provider][authMode] ?? RATE_LIMIT_PATTERNS[provider].oauth ?? [];
}

const STRUCTURED_429_PATTERNS = ['"api_error_status":429', '"error":"rate_limit"'] as const;

function hasRateLimitProviderContext(text: string, provider: ProviderName): boolean {
  return RATE_LIMIT_PROVIDER_CONTEXT_PATTERNS[provider].some(p => text.includes(p));
}

export function matchesRateLimit(text: string, provider: ProviderName = PROVIDER, authMode: AuthMode = AUTH_MODE): boolean {
  const lower = text.toLowerCase();
  if (getRateLimitPatterns(provider, authMode).some(p => lower.includes(p))) return true;
  if (
    authMode === 'api_key'
    && (provider === 'openai' || provider === 'zai' || provider === 'deepseek')
    && hasRateLimitProviderContext(lower, provider)
    && API_KEY_CAPACITY_PATTERNS.some(p => lower.includes(p))
  ) {
    return true;
  }
  return STRUCTURED_429_PATTERNS.some(p => text.includes(p));
}

// Authentication-failure signatures emitted by a provider CLI when the supplied
// credential is ABSENT, MALFORMED, EXPIRED, or REVOKED — i.e. the account is not
// authenticated at all. This is categorically different from a rate-limit (a
// valid account that is throttled). When the pre-task credential validation hits
// one of these, the VM has no usable auth source: the task must be reported as a
// provisioning/auth pre-claim failure and NOT consumed as a code failure (which
// would burn the retry budget on a credential problem no code edit can fix).
//
// NOTE on `apiKeySource:"none"`: this is NOT a rejection signal and must NOT be
// matched here. The project's documented default auth mode is a Max-plan
// subscription / OAuth session (.env.example AUTH_MODE=oauth), which uses no API
// key — so the Claude CLI init event reports `"apiKeySource":"none"` in EVERY
// init event, including perfectly healthy ones. Matching it would misclassify any
// non-zero-exit validation on a healthy OAuth VM (transient overload / network /
// timeout) as an auth rejection and fail the VM pre-claim. A genuine rejection is
// always accompanied by an explicit signal — the HTTP 401 `authentication_failed`
// body or the provider-specific patterns below — so those carry full detection
// coverage on their own. Patterns are checked case-insensitively against the
// combined stdout+stderr of the validation CLI, so every entry is lowercase.
const AUTH_FAILURE_PATTERNS_GENERIC: readonly string[] = [
  'authentication_failed',
  'authentication failed',
  'failed to authenticate',
  'authentication error',
  'invalid authentication credentials',
  'invalid api key',
  'invalid bearer token',
  'bad credentials',
  'token expired',
  'token invalid',
  'token revoked',
  // OAuth refresh-token death: the stored refresh token was already
  // consumed/rotated, so the access token can no longer be refreshed and the
  // account needs an interactive re-auth — NOT a transient retry. Production
  // 2026-06-14: SavarinCodex (the sole live Codex account) emitted these
  // `refresh_token_reused` 401s; without coverage here matchesAuthFailure
  // missed it, so runClaudeCode returned a bare non-zero exit instead of
  // without coverage here matchesAuthFailure missed it so runClaudeCode returned
  // a bare non-zero exit instead of throwing AuthFailureError. These markers
  // route the crash to the standard retryable
  // auth-failure path (account auto-disabled + task requeued onto a live one).
  'refresh_token_reused',
  'refresh token has already been used',
  'refresh token was already used',
  'please log out and sign in again',
] as const;

const AUTH_FAILURE_PATTERNS: Readonly<Record<ProviderName, readonly string[]>> = {
  anthropic: [
    'invalid x-api-key',
    'x-api-key header is required',
    'organization has disabled claude subscription access',
    'oauth token has expired',
    'oauth token expired',
    'oauth authentication is currently not supported',
    'oauth_org_not_allowed',
    'please run /login',
    'could not resolve authentication',
  ],
  openai: [
    'invalid_api_key',
    'incorrect api key provided',
    "you didn't provide an api key",
    'invalid authentication',
    'missing bearer or basic authentication',
  ],
  gemini: [
    'api key not valid',
    'api_key_invalid',
    'unauthenticated',
    'permission_denied',
    'invalid authentication credentials',
  ],
  zai: [
    'api key not valid',
    'invalid z.ai api key',
    'invalid zai api key',
    'missing api key',
    'x-api-key header is required',
    'invalid x-api-key',
  ],
  deepseek: [
    'api key not valid',
    'invalid deepseek api key',
    'missing api key',
    'x-api-key header is required',
    'invalid x-api-key',
    'authentication fails',
  ],
} as const;

export function matchesAuthFailure(text: string, provider: ProviderName = PROVIDER): boolean {
  const lower = text.toLowerCase();
  if (AUTH_FAILURE_PATTERNS_GENERIC.some(p => lower.includes(p))) return true;
  return AUTH_FAILURE_PATTERNS[provider].some(p => lower.includes(p));
}

// VM-config-missing signatures: the provider CLI died because a
// boot/retarget-materialized config FILE is absent from the VM — not because
// of anything the account or the code under test did. The canonical case is
// the Claude CLI's instant death when ~/.claude.json (the onboarding-bypass
// config written by materialize_manifest.py) has been destroyed: it exits
// within seconds printing "Claude configuration file not found" and leaves a
// ~/.claude/backups/.claude.json.backup.<epoch-ms> file behind. Production
// 2026-06-06 (tasks 1330d291 / cf28097f on VMs dfa09f9f / 312537c4): a warm
// account-divergent re-target died after clearing credentials but before
// re-materializing, and every task the poisoned VM claimed died this way while
// the orchestrator misread the init-banner-only output tail as usage
// exhaustion. Kept deliberately narrow — this routes to an infra class that
// bypasses the code-error retry budget, so a loose match would hide real
// defects.
const VM_CONFIG_MISSING_PATTERNS: readonly string[] = [
  'claude configuration file not found',
];

export function matchesVmConfigMissing(text: string): boolean {
  const lower = text.toLowerCase();
  return VM_CONFIG_MISSING_PATTERNS.some(p => lower.includes(p));
}

// Boot-materialized config files the provider CLI cannot start without.
// anthropic: ~/.claude.json is the onboarding-bypass file — without it the
// Claude CLI dies in seconds regardless of valid OAuth/API-key env. Other
// providers have no CONFIRMED hard file requirement (codex auth rides
// CODEX_HOME/env; gemini can run on GEMINI_API_KEY alone), so they are left
// empty rather than risking false VM dooms — extend per provider as
// requirements are confirmed.
export function requiredProviderConfigFiles(provider: ProviderName = PROVIDER): string[] {
  if (provider === 'anthropic') {
    // HOME-anchored: the Claude CLI reads ~/.claude.json from $HOME. Fall back to
    // os.homedir() (never a fixed /home/agent) so the check tracks the actual
    // home — /home/agent on the cloud VM, /home inside the fleet-runner sandbox.
    const home = process.env.HOME || os.homedir();
    return [`${home}/.claude.json`];
  }
  return [];
}

/** Required provider config files (see above) that are absent from disk. */
export function missingProviderConfigFiles(provider: ProviderName = PROVIDER): string[] {
  return requiredProviderConfigFiles(provider).filter(p => {
    try {
      return !fs.existsSync(p);
    } catch {
      // A failed stat must not produce a false "missing" doom signal.
      return false;
    }
  });
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

export class AuthFailureError extends Error {
  readonly failureCategory = 'external_api' as const;

  constructor(message: string) {
    super(message);
    this.name = 'AuthFailureError';
  }
}

// Thrown when the provider CLI died because a boot/retarget-materialized config
// file is missing from the VM (VM_CONFIG_MISSING_PATTERNS matched on a non-zero
// exit). This is a VM/infra fault — the account is healthy and no code edit can
// fix it — so it carries its own failureCategory ('vm_config_missing' →
// PROVISION_FAILURE backend-side: requeue on a fresh VM, no code-error retry
// burn, no account cooldown). Distinct from the generic 'claude_code_failed' /
// 'fatal' labels, whose init-banner-only output tail the orchestrator
// reclassifies as usage exhaustion (RATE_LIMIT) — exactly the misread the
// 2026-06-06 poisoned-VM incidents produced.
export class VmConfigMissingError extends Error {
  readonly failureCategory = 'vm_config_missing' as const;

  constructor(message: string) {
    super(message);
    this.name = 'VmConfigMissingError';
  }
}

// ── Structured provider-run error report ─────────────────────────────────────
// The truthful record of HOW the last provider CLI run terminated, captured at
// the runner layer (which actually observes the exit code, stderr, and the
// provider error events in the stream) instead of being reverse-engineered
// from output-text tails by the backend. Recorded for EVERY run — success or
// failure — and shipped with the terminal FAILED status post (see
// enrichStatusPayload in index.ts), so backend failure classification can
// consume it as the PRIMARY signal and only fall back to text heuristics for
// old agents that never sent one. Provider-agnostic by construction: each
// provider's stream parser feeds the same fields (model_output_seen /
// result_event_seen / provider_errors), so a provider error routes to the
// rate-limit/transient path without per-provider backend regexes.
// Incident 2026-06-06: a create task died 5s after CLI start (exit 1, output
// tail = init banner only — a provider error on the first API call) and was
// classified CODE_ERROR, stripping rate-limit handling and burning all 3
// retries in seconds.
export interface ProviderErrorEntry {
  /** Which stream surface emitted the error (e.g. 'claude-stream', 'codex-turn-failed'). */
  source: string;
  /** Provider error code/type when the event carried one. */
  code?: string;
  message: string;
}

export interface ProviderRunErrorReport {
  schema: 1;
  /** Task the run belonged to — guards against stale cross-role reuse on warm VMs. */
  task_id: string;
  provider: ProviderName;
  auth_mode: AuthMode;
  exit_code: number | null;
  exit_signal: string | null;
  duration_ms: number;
  /** True once the model produced ANY top-level output (delta/assistant/result). */
  model_output_seen: boolean;
  /** True once the terminal result event arrived (claude `result` / codex `turn.completed`). */
  result_event_seen: boolean;
  /** Structured provider error events observed in the stream (bounded). */
  provider_errors: ProviderErrorEntry[];
  stderr_tail: string;
  /** Last parsed stream-json event type, for forensics. */
  last_stream_event: string | null;
  /** Agent-side matcher verdict when one fired (rate_limit / auth_failure / …). */
  classified?: string;
}

const RUN_REPORT_STDERR_TAIL_CHARS = 2000;
const RUN_REPORT_MAX_PROVIDER_ERRORS = 5;
const RUN_REPORT_ERROR_MESSAGE_CHARS = 500;

let _lastRunErrorReport: ProviderRunErrorReport | null = null;

/** The structured report for the most recent provider run, or null if none ran yet. */
export function getLastProviderRunErrorReport(): ProviderRunErrorReport | null {
  return _lastRunErrorReport;
}

/** Clear the last-run report. Called from resetPerRoleModuleState so a warm
 *  role can never observe the prior role's run report through the getter
 *  (enrichStatusPayload also guards attachment by task_id). */
export function clearLastProviderRunErrorReport(): void {
  _lastRunErrorReport = null;
}

/** Test-only: seed/clear the last-run report. */
export function _setLastProviderRunErrorReportForTesting(report: ProviderRunErrorReport | null): void {
  _lastRunErrorReport = report;
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

/**
 * Optional, public-safe observation of a one-shot provider run.
 *
 * The runner continues to own its normal log, telemetry, retry, and error
 * behavior.  A caller may opt into these finalized assistant text and top-level
 * tool-use notifications to mirror an operator-visible turn elsewhere (for
 * example, the typed `strategic_session` WebSocket stream).  Deliberately do
 * not surface partial/thinking events: they can contain private reasoning and
 * are not a durable public reply contract.
 */
export interface ProviderRunObserver {
  onAssistantText?: (text: string) => void;
  onToolUse?: (name: string, input: unknown) => void;
}

/** Narrow execution contract for a receipt-only memory worker.
 *
 * It is deliberately not a general sandbox toggle: callers must supply a
 * freshly-created private directory, and only the native Claude runner is
 * admitted until another provider can demonstrate an equivalent no-tool
 * allowlist.  This fails closed rather than silently widening a sensitive
 * task's capabilities on a provider change.
 */
export interface IsolatedTextOnlyRunOptions {
  cwd: string;
  timeoutMs: number;
  maxOutputBytes: number;
}

export class TextOnlyProviderUnavailableError extends RuntimeUnavailableError {
  constructor(provider: ProviderName, detail: string) {
    // RuntimeUnavailableError is the control type runRoleOnce converts to the
    // graceful exit-42 path. Preserve the more specific diagnostic text while
    // sharing that control identity so a strict text-only capability miss does
    // not increment the local runner's failure streak.
    super(`${provider}:${detail}`);
    this.message = `text_only_provider_unavailable:${provider}:${detail}`;
    this.name = 'TextOnlyProviderUnavailableError';
  }
}

const TEXT_ONLY_MAX_TURNS = 1;

function removeClaudeFlags(args: readonly string[], flags: ReadonlySet<string>): string[] {
  const clean: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (!flags.has(arg)) {
      clean.push(arg);
      continue;
    }
    index += CLAUDE_OPTIONAL_FLAG_VALUE_COUNTS[arg] ?? 0;
  }
  return clean;
}

function isolatedAnthropicEnv(baseEnv: Record<string, string>, cwd: string): Record<string, string> {
  // Do not inherit the agent's HOME, repository credentials, generic helper
  // path, or unrelated provider credentials.  The selected provider token is
  // enough for the supported text-only path; a CLI that requires more ambient
  // state simply fails closed.
  const env: Record<string, string> = {
    HOME: cwd,
    PATH: baseEnv.PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: baseEnv.LANG || 'en_US.UTF-8',
    TERM: baseEnv.TERM || 'xterm-256color',
  };
  for (const key of ['CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'] as const) {
    if (baseEnv[key]) env[key] = baseEnv[key];
  }
  return env;
}

/** Build the testable command contract used by memory consolidation only. */
export function buildIsolatedTextOnlyMemoryCommand(
  runner: ProviderRunner,
  prompt: string,
  model: string | undefined,
  effort: string | undefined,
  options: IsolatedTextOnlyRunOptions,
): RunnerCommand {
  if (runner.provider !== 'anthropic') {
    throw new TextOnlyProviderUnavailableError(runner.provider, 'no_verified_text_only_runner');
  }
  const command = runner.buildRunCommand(prompt, model, effort);
  if (command.command !== 'claude') {
    throw new TextOnlyProviderUnavailableError(runner.provider, 'unexpected_cli');
  }
  // The regular command opts into blanket permissions for coding work. The
  // receipt worker removes every inherited tool/config flag and uses the
  // explicit empty built-in list plus a fresh empty strict-MCP config. Note
  // that --allowedTools controls approval, not availability: --tools '' is
  // the actual built-in-tool boundary.
  command.args = removeClaudeFlags(command.args, new Set([
    '--dangerously-skip-permissions',
    CLAUDE_PARTIAL_MESSAGES_FLAG,
    '--tools',
    '--allowedTools',
    '--mcp-config',
    '--strict-mcp-config',
    '--max-turns',
  ]));
  const mcpConfigPath = path.join(options.cwd, '.memory-empty-mcp.json');
  writePrivateFileSync(mcpConfigPath, '{"mcpServers":{}}\n');
  command.args.push(
    '--tools', '',
    '--mcp-config', mcpConfigPath,
    '--strict-mcp-config',
    '--allowedTools', '',
    '--max-turns', String(TEXT_ONLY_MAX_TURNS),
  );
  command.env = isolatedAnthropicEnv(command.env, options.cwd);
  command.timeoutMs = Math.min(command.timeoutMs, options.timeoutMs);
  return command;
}

// These values are execution authority chosen by the local runner. They may
// cross the provider-CLI boundary, but an operator-supplied translation map
// must never synthesize them when the runner did not provide them.
const RUNNER_AUTHORED_CLI_ENV_KEYS: ReadonlySet<string> = new Set([
  'BLUEPRINT_SANDBOX_PYTHON',
  'BLUEPRINT_SANDBOX_PG_BINDIR',
  'VITEST_MIN_THREADS',
  'VITEST_MAX_THREADS',
  'VITEST_MIN_FORKS',
  'VITEST_MAX_FORKS',
  'VITEST_MIN_WORKERS',
  'VITEST_MAX_WORKERS',
]);

function buildBaseCliEnv(provider: ProviderName = PROVIDER): Record<string, string> {
  const env: Record<string, string> = {
    HOME: process.env.HOME || os.homedir(),
    PATH: process.env.PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: process.env.LANG || 'en_US.UTF-8',
    TERM: process.env.TERM || 'xterm-256color',
  };
  for (const key of [
    'CLAUDE_CODE_OAUTH_TOKEN',
    'CLAUDE_CODE_EFFORT_LEVEL',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
    'ZAI_API_KEY',
    'ZAI_ANTHROPIC_BASE_URL',
    'ZAI_BASE_URL',
    'DEEPSEEK_API_KEY',
    'DEEPSEEK_ANTHROPIC_BASE_URL',
    'DEEPSEEK_BASE_URL',
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
    // Runner-authored backend verification and test-execution policy. Shell
    // tools are children of the provider CLI, not the agent coordinator.
    ...RUNNER_AUTHORED_CLI_ENV_KEYS,
  ]) {
    const v = process.env[key];
    if (v !== undefined) env[key] = v;
  }
  const agentToken = getAgentToken();
  if (agentToken) env.AGENT_API_TOKEN = agentToken;
  // Propagate the live token-file path so the provisioned bash helpers read the
  // rotated token FRESH on each invocation (see token-file.ts). The path is
  // stable; only the file's CONTENTS change on refresh, so passing it once at
  // spawn is correct even for a long-lived subprocess. AGENT_API_TOKEN
  // above stays as the fallback for the one-shot case / when the file is unset.
  const tokenFilePath = process.env[AGENT_TOKEN_FILE_ENV];
  if (tokenFilePath) env[AGENT_TOKEN_FILE_ENV] = tokenFilePath;
  const ghTokenFilePath = process.env[GITHUB_TOKEN_FILE_ENV];
  if (ghTokenFilePath) env[GITHUB_TOKEN_FILE_ENV] = ghTokenFilePath;
  if (LLM_CLI_TRANSLATION?.env) {
    for (const [k, v] of Object.entries(LLM_CLI_TRANSLATION.env)) {
      if (!(k in env) && !RUNNER_AUTHORED_CLI_ENV_KEYS.has(k)) env[k] = v;
    }
  }
  if (provider === 'zai') {
    if (env.ZAI_API_KEY) {
      env.ANTHROPIC_API_KEY = env.ZAI_API_KEY;
      env.ANTHROPIC_AUTH_TOKEN = env.ZAI_API_KEY;
    } else {
      delete env.ANTHROPIC_API_KEY;
      delete env.ANTHROPIC_AUTH_TOKEN;
    }
    const baseUrl = env.ZAI_ANTHROPIC_BASE_URL || env.ZAI_BASE_URL || env.ANTHROPIC_BASE_URL || ZAI_ANTHROPIC_BASE_URL;
    env.ANTHROPIC_BASE_URL = baseUrl.replace(/\/+$/, '');
  }
  if (provider === 'deepseek') {
    if (env.DEEPSEEK_API_KEY) {
      env.ANTHROPIC_API_KEY = env.DEEPSEEK_API_KEY;
      env.ANTHROPIC_AUTH_TOKEN = env.DEEPSEEK_API_KEY;
    } else {
      delete env.ANTHROPIC_API_KEY;
      delete env.ANTHROPIC_AUTH_TOKEN;
    }
    const baseUrl = env.DEEPSEEK_ANTHROPIC_BASE_URL || env.DEEPSEEK_BASE_URL || env.ANTHROPIC_BASE_URL || DEEPSEEK_ANTHROPIC_BASE_URL;
    env.ANTHROPIC_BASE_URL = baseUrl.replace(/\/+$/, '');
    // Documented model-selection env (see deepseek-env.ts for the full
    // rationale, precedence, and why it is scoped to this branch only).
    applyDeepSeekModelEnv(env);
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
  if (provider === 'zai' || provider === 'deepseek') authMode = 'api_key';
  const baseEnv = buildBaseCliEnv(provider);
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
  const isZai = provider === 'zai';
  const isDeepSeek = provider === 'deepseek';
  const claudeDisplayName = isZai
    ? 'Z.AI GLM (Claude compat)'
    : isDeepSeek
      ? 'DeepSeek (Claude compat)'
      : 'Claude Code';
  return {
    provider: isZai ? 'zai' : isDeepSeek ? 'deepseek' : 'anthropic',
    authMode,
    displayName: claudeDisplayName,
    credentialSummary: () => {
      if (isZai) return 'Using ZAI_API_KEY via the Anthropic-compatible endpoint';
      if (isDeepSeek) return 'Using DEEPSEEK_API_KEY via the Anthropic-compatible endpoint';
      return baseEnv.CLAUDE_CODE_OAUTH_TOKEN ? 'Using OAuth token for Claude Code auth' : 'Using ANTHROPIC_API_KEY as fallback auth';
    },
    missingCredentials: () => {
      if (isZai) return baseEnv.ZAI_API_KEY ? [] : ['ZAI_API_KEY'];
      if (isDeepSeek) return baseEnv.DEEPSEEK_API_KEY ? [] : ['DEEPSEEK_API_KEY'];
      return (baseEnv.CLAUDE_CODE_OAUTH_TOKEN || baseEnv.ANTHROPIC_API_KEY) ? [] : ['CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY'];
    },
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
      let effectiveModel = model;
      if (LLM_CLI_TRANSLATION) {
        // Older installed Claude Code binaries reject --effort even though
        // current releases document it. CLAUDE_CODE_EFFORT_LEVEL is the
        // supported, higher-precedence control and is safe for older binaries
        // to ignore, so translate the generic flag into the environment.
        const translatedArgs: string[] = [];
        for (let i = 0; i < LLM_CLI_TRANSLATION.args.length; i += 1) {
          if (LLM_CLI_TRANSLATION.args[i] === '--effort') {
            if (i + 1 < LLM_CLI_TRANSLATION.args.length) {
              effectiveEffort = LLM_CLI_TRANSLATION.args[i + 1];
              i += 1;
            }
            continue;
          }
          translatedArgs.push(LLM_CLI_TRANSLATION.args[i]);
        }
        args.push(...translatedArgs);
        const modelIdx = LLM_CLI_TRANSLATION.args.indexOf('--model');
        if (modelIdx >= 0 && modelIdx + 1 < LLM_CLI_TRANSLATION.args.length) {
          effectiveModel = LLM_CLI_TRANSLATION.args[modelIdx + 1];
        }
      } else {
        if (model) args.push('--model', model);
      }
      if (disallowedTools && disallowedTools.length > 0) {
        for (const tool of disallowedTools) args.push('--disallowedTools', tool);
      }
      const claudeEnv = { ...baseEnv };
      if (effectiveEffort && !claudeEnv.CLAUDE_CODE_EFFORT_LEVEL) {
        claudeEnv.CLAUDE_CODE_EFFORT_LEVEL = effectiveEffort;
      }
      return {
        command: 'claude',
        args,
        env: pinDeepSeekModel(claudeEnv, isDeepSeek, effectiveModel),
        outputMode: 'claude-stream-json',
        timeoutMs: claudeTimeoutForEffort(effectiveEffort),
        promptInput: 'stdin-file',
        displayName: claudeDisplayName,
      };
    },
    buildValidationCommand: (model) => {
      // Name the pool-selected model: a bare probe proves only that the key
      // authenticates, so AGENT_AUTH_OK could pass on a broken model (the
      // DeepSeek broken-model failure mode).
      const args = ['-p', 'respond with OK', '--max-turns', '1', '--output-format', 'stream-json', '--dangerously-skip-permissions'];
      if (model) args.push('--model', model);
      return {
        command: 'claude',
        args,
        env: pinDeepSeekModel(baseEnv, isDeepSeek, model),
        timeoutMs: 30_000,
      };
    },
  };
}

type RunClaudeCodeOverride = (
  prompt: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean,
  observer?: ProviderRunObserver,
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

/**
 * Provision the small set of "ambient" helper CLIs that EVERY model task may use
 * regardless of which handler dispatched it: ./note (annotations), ./playbook
 * (shared collective memory), and ./messages (operator channel). The prompt
 * subsystem advertises these broadly — DEFAULT_TOOL_GUIDANCE references ./note
 * and ./messages, PLAYBOOK_GUIDANCE references ./playbook — and they are emitted
 * by read-only research-family handlers (web_research, design, memo) that do not
 * provision them themselves. Writing them here once, for every provider run,
 * guarantees the prompt/helper contract holds for those handlers and closes the
 * whole drift class: a future handler cannot forget to provision them.
 *
 * Best-effort and idempotent. Every filesystem op is guarded so a provisioning
 * failure never aborts the model run, and the writers overwrite, so handlers
 * that ALSO provision these in-handler (code-change, research, strategic) are
 * unaffected. The workspace is created first (recursive) because the no-clone
 * handlers (memo, web_research) can run before any directory exists — a plain
 * existsSync guard would silently skip provisioning and leave their advertised
 * helpers missing. ./note reads TASK_ID from the environment at run time, so it
 * is always written; ./playbook and ./messages bake the task id into the script
 * body, so they are written only when a task id is available.
 */
export function provisionAmbientHelpers(taskId: string, workspace: string = WORKSPACE): void {
  try {
    fs.mkdirSync(workspace, { recursive: true });
  } catch { /* best effort — the guarded writes below still attempt to run */ }

  try {
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
TASK="\${TASK_ID:?TASK_ID not set}"
${agentTokenShellFn()}
_load_agent_token || exit 1
${retryPostShellFn()}
${jsonStrShellFn()}

# Build JSON with _json_str to avoid shell escaping issues
JSON=$(printf '{"task_id":%s,"annotation_type":%s,"content":%s' \\
  "$(_json_str "$TASK")" "$(_json_str "$TYPE")" "$(_json_str "$CONTENT")")
[ -n "$PAGE" ] && JSON="\${JSON},\\"target_page\\":$(_json_str "$PAGE")"
[ -n "$SEVERITY" ] && JSON="\${JSON},\\"severity\\":$(_json_str "$SEVERITY")"
JSON="\${JSON}}"

printf '%s' "$JSON" | _post_with_retry "$API/api/agent/annotate" "note $TYPE"
rc=$?
echo ""
exit $rc
`;
    fs.writeFileSync(`${workspace}/note`, noteScript, { mode: 0o755 });
    try {
      const excludePath = `${workspace}/.git/info/exclude`;
      const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
      if (!existing.includes('/note')) {
        fs.appendFileSync(excludePath, '/note\n');
      }
    } catch { /* shallow clone may lack .git/info */ }
  } catch { /* note provisioning is best-effort */ }

  // ./playbook and ./messages bake the task id into the script body, so only
  // provision them when one is available. Idempotent overwrites; the writers
  // call excludeAgentHelpers() so the files stay git-ignored.
  if (taskId) {
    try { writePlaybookHelper(taskId, workspace); } catch { /* best effort */ }
    try { writeMessagesHelper(taskId, workspace); } catch { /* best effort */ }
    try { writeSymbolsHelper(taskId, workspace); } catch { /* best effort */ }
  }
}

/**
 * Mirror generated workspace helpers into a dedicated PATH directory.
 *
 * Root-level ``./helper`` files remain for compatibility, while bare helper
 * names keep working after an agent changes into a repository subdirectory.
 * Only the explicit helper allowlist is copied; arbitrary repository
 * executables never enter PATH.
 */
export function syncAgentHelpersToPath(
  workspace: string,
  env: Record<string, string>,
): string {
  const taskKey = (process.env.TASK_ID || 'current').replace(/[^a-zA-Z0-9_-]/g, '_');
  const helperBin = path.join(os.tmpdir(), `blueprint-agent-bin-${taskKey}`);
  // Node always provides rmSync in the supported runtime. Some focused tests
  // intentionally install a narrow fs facade because they exercise provider
  // streaming rather than filesystem behavior. Allow that facade only when it
  // proves there is no stale helper directory to clear; otherwise fail closed
  // rather than leaving wrappers from an earlier task on PATH.
  if (typeof fs.rmSync === 'function') {
    fs.rmSync(helperBin, { recursive: true, force: true });
  } else if (fs.existsSync(helperBin)) {
    throw new Error(
      `Cannot safely reset agent helper PATH directory without fs.rmSync: ${helperBin}`,
    );
  }
  fs.mkdirSync(helperBin, { recursive: true, mode: 0o700 });
  for (const name of AGENT_HELPER_NAMES) {
    const source = path.join(workspace, name);
    const target = path.join(helperBin, name);
    try {
      const stat = fs.lstatSync(source);
      if (!stat.isFile()) continue;
      const quotedSource = `'${source.replace(/'/g, `'"'"'`)}'`;
      // A wrapper preserves script-relative workspace behavior. Copying helper
      // bytes would break helpers such as `artifacts`, which intentionally
      // locate .blueprint-inputs relative to their own workspace path.
      fs.writeFileSync(
        target,
        `#!/bin/sh\nexec ${quotedSource} "$@"\n`,
        { mode: 0o755 },
      );
    } catch {
      // A helper not provisioned for this task type is intentionally absent.
    }
  }
  const currentPath = env.PATH || '/usr/local/bin:/usr/bin:/bin';
  env.PATH = `${helperBin}${path.delimiter}${currentPath}`;
  return helperBin;
}

export async function runClaudeCode(prompt: string, ws: WebSocket | null, statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean, observer?: ProviderRunObserver): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
  codexTurnCompleted?: boolean;
  stderrTail?: string;
}> {
  if (_runClaudeCodeOverride) {
    return _runClaudeCodeOverride(prompt, ws, statusRef, model, effort, disallowedTools, enableCheckWatchdog, observer);
  }
  return runProviderAgent(prompt, ws, statusRef, model, effort, disallowedTools, enableCheckWatchdog, undefined, observer);
}

/** Append text without exceeding a byte ceiling (JS string length is UTF-16). */
function appendWithinByteLimit(current: string, chunk: string, maxBytes: number): string {
  const used = Buffer.byteLength(current, 'utf8');
  const remaining = maxBytes - used;
  if (remaining <= 0) return current;
  if (Buffer.byteLength(chunk, 'utf8') <= remaining) return current + chunk;
  let low = 0;
  let high = chunk.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (Buffer.byteLength(chunk.slice(0, middle), 'utf8') <= remaining) low = middle;
    else high = middle - 1;
  }
  return current + chunk.slice(0, low);
}

export async function runIsolatedTextOnlyMemory(
  prompt: string,
  ws: WebSocket | null,
  statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number },
  model: string | undefined,
  effort: string | undefined,
  options: IsolatedTextOnlyRunOptions,
): Promise<{ exitCode: number; allOutput: string; lastResultText: string; codexTurnCompleted?: boolean; stderrTail?: string }> {
  return runProviderAgent(prompt, ws, statusRef, model, effort, undefined, false, options);
}

export async function runProviderAgent(prompt: string, ws: WebSocket | null, statusRef: { value: string; toolStartedAt?: number; lastProgressAt?: number }, model?: string, effort?: string, disallowedTools?: string[], enableCheckWatchdog?: boolean, isolatedTextOnly?: IsolatedTextOnlyRunOptions, observer?: ProviderRunObserver): Promise<{
  exitCode: number; allOutput: string; lastResultText: string;
  codexTurnCompleted?: boolean;
  stderrTail?: string;
}> {
  const runner = getProviderRunner();
  const runCommand = isolatedTextOnly
    ? buildIsolatedTextOnlyMemoryCommand(runner, prompt, model, effort, isolatedTextOnly)
    : runner.buildRunCommand(prompt, model, effort, disallowedTools);
  let allOutput = '';
  // Set once the raw stream passes the byte cap in a (non-isolated) structured
  // mode: allOutput freezes at the cap and subsequent chunks land in this
  // rolling tail, which is re-joined to allOutput after the child exits.
  let allOutputTail = '';
  let allOutputTailMode = false;
  let lastResultText = '';
  let stderrBuffer = '';
  // Structured-run-report tracking (see ProviderRunErrorReport). Fed by every
  // provider's stream parser so the terminal report is provider-agnostic.
  const runStartedAt = Date.now();
  let modelOutputSeen = false;
  let resultEventSeen = false;
  let lastStreamEventType: string | null = null;
  let closeSignalSeen: string | null = null;
  const providerErrors: ProviderErrorEntry[] = [];
  const recordProviderError = (source: string, message: string, code?: string): void => {
    if (providerErrors.length >= RUN_REPORT_MAX_PROVIDER_ERRORS) return;
    providerErrors.push({
      source,
      message: sanitize(message).slice(0, RUN_REPORT_ERROR_MESSAGE_CHARS),
      ...(code ? { code: code.slice(0, 100) } : {}),
    });
  };
  // Record the structured run report for THIS run (success or failure). Called
  // on every exit path — including just before each typed throw — so whoever
  // posts the terminal status always has the truthful exit context to ship.
  const recordRunErrorReport = (exitCode: number | null, classified?: string): void => {
    // Text-mode providers (gemini) have no terminal result event; a clean exit
    // that produced output is their completed-run signal.
    const completedTextRun =
      runCommand.outputMode === 'text' && exitCode === 0 && modelOutputSeen;
    _lastRunErrorReport = {
      schema: 1,
      task_id: process.env.TASK_ID || '',
      provider: runner.provider,
      auth_mode: runner.authMode,
      exit_code: exitCode,
      exit_signal: closeSignalSeen,
      duration_ms: Date.now() - runStartedAt,
      model_output_seen: modelOutputSeen,
      result_event_seen: resultEventSeen || completedTextRun,
      provider_errors: providerErrors,
      stderr_tail: stderrBuffer.slice(-RUN_REPORT_STDERR_TAIL_CHARS),
      last_stream_event: lastStreamEventType,
      ...(classified ? { classified } : {}),
    };
  };
  const flags: { timedOut: boolean; outputLimited: boolean; providerKilledByOOM: boolean; stalledMidStream: boolean; llmNoProgressHang: boolean; agentKillIssued: boolean } = { timedOut: false, outputLimited: false, providerKilledByOOM: false, stalledMidStream: false, llmNoProgressHang: false, agentKillIssued: false };
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
    if (isProgress && toolInFlight) {
      lastToolProgressAt = Date.now();
      statusRef.lastProgressAt = Date.now();
    }
  };
  let lastCheckOutputOffset = -1;
  let codexTurnCompleted = false;

  const promptPreview = isolatedTextOnly
    ? '[receipt-only prompt redacted]'
    : `${prompt.slice(0, 200)}${prompt.length > 200 ? '...' : ''}`;
  sendLog(ws, `\n--- ${runner.displayName} starting ---\nPrompt: ${promptPreview}\nMode: ${LLM_EXECUTION_MODE}\n\n`);
  log(`Spawning ${runner.displayName} CLI...`);

  // Pre-flight: verify the CLI binary is present before spawning. A missing
  // binary (e.g. `codex` not installed on this runner) is an infra decline —
  // throw RuntimeUnavailableError so the agent exits with code 42 and the
  // runner does NOT increment its failure streak.
  const cliCommand = runCommand.command;
  if (!_commandExists(cliCommand)) {
    sendLog(ws, `[preflight] CLI binary '${cliCommand}' not found on PATH — declining task (runtime_unavailable)\n`);
    throw new RuntimeUnavailableError(cliCommand);
  }

  // Claude-compatible CLIs vary by installed version. Negotiate their optional
  // flags before launch so an old binary reaches the model instead of rejecting
  // argv locally. Required model/provider routing is never part of this filter.
  let partialMessageProgressAvailable = runCommand.args.includes(CLAUDE_PARTIAL_MESSAGES_FLAG);
  if (cliCommand === 'claude') {
    const capabilities = probeClaudeCapabilities(cliCommand, runCommand.env);
    if (isolatedTextOnly && (
      capabilities === null
      || !capabilities.has('--tools')
      || !capabilities.has('--mcp-config')
      || !capabilities.has('--strict-mcp-config')
      || !capabilities.has('--allowedTools')
    )) {
      throw new TextOnlyProviderUnavailableError(
        runner.provider,
        'cli_cannot_prove_toolless_strict_mcp_turn_cap',
      );
    }
    // --max-turns was removed from Claude Code as of 2.1.x. A toolless turn
    // cannot extend itself (no tool results to continue on) and the wrapper's
    // wall-clock timeout still bounds the process, so the turn cap is required
    // only when the installed CLI can express it.
    const cliSupportsMaxTurns = capabilities?.has('--max-turns') ?? false;
    const compatibleArgs = stripUnsupportedClaudeOptionalArgs(runCommand.args, capabilities);
    if (isolatedTextOnly && (
      !compatibleArgs.includes('--tools')
      || !compatibleArgs.includes('--mcp-config')
      || !compatibleArgs.includes('--strict-mcp-config')
      || !compatibleArgs.includes('--allowedTools')
      || (cliSupportsMaxTurns && !compatibleArgs.includes('--max-turns'))
      || compatibleArgs.includes('--dangerously-skip-permissions')
    )) {
      throw new TextOnlyProviderUnavailableError(
        runner.provider,
        'tool_or_turn_boundary_removed_by_compatibility_filter',
      );
    }
    if (isolatedTextOnly && !cliSupportsMaxTurns) {
      sendLog(ws, '[preflight] Claude CLI has no --max-turns; toolless turn proceeds under timeout bound only\n');
    }
    const removedPartialMessages = partialMessageProgressAvailable
      && !compatibleArgs.includes(CLAUDE_PARTIAL_MESSAGES_FLAG);
    runCommand.args = compatibleArgs;
    partialMessageProgressAvailable = compatibleArgs.includes(CLAUDE_PARTIAL_MESSAGES_FLAG);
    if (removedPartialMessages) {
    sendLog(
      ws,
      `[preflight] Claude CLI does not support ${CLAUDE_PARTIAL_MESSAGES_FLAG}; using compatible stream mode\n`,
    );
    }
  }

  // OAuth accounts may have earned Codex rate-limit reset credits. Consume one
  // exact credit only when the real seven-day window is exhausted. Exact IDs
  // make concurrent starts converge on one redemption, while probe failures
  // fail open to the normal Codex CLI classification path.
  if (!isolatedTextOnly && runner.provider === 'openai' && runner.authMode === 'oauth') {
    statusRef.value = 'Checking Codex usage';
    const reset = await maybeConsumeCodexWeeklyReset({
      command: cliCommand,
      env: runCommand.env,
    });
    if (reset.status === 'reset') {
      sendLog(ws, '[codex-usage] Seven-day usage was exhausted; consumed one available reset credit and continuing.\n');
    } else if (reset.status === 'no_credit' && reset.exhausted) {
      const detail = reset.availableCreditCount > 0 && !reset.creditDetailsAvailable
        ? 'credit details unavailable, so automatic redemption was skipped safely'
        : 'no reset credit is available';
      sendLog(ws, `[codex-usage] Seven-day usage is exhausted; ${detail}.\n`);
    } else if (reset.status === 'probe_failed') {
      sendLog(ws, `[codex-usage] Usage-reset preflight unavailable (${sanitize(reset.reason)}); continuing with standard Codex handling.\n`);
    }
  }

  // Provision the ambient helper CLIs (./note, ./playbook, ./messages) that
  // every model task may use regardless of handler. Self-guarding and
  // idempotent — see provisionAmbientHelpers.
  if (!isolatedTextOnly) {
    provisionAmbientHelpers(process.env.TASK_ID || '', WORKSPACE);
    syncAgentHelpersToPath(WORKSPACE, runCommand.env);
  }

  const exitCode = await new Promise<number>((resolve) => {
    let promptFile = '';
    let promptFd: number | undefined;
    let child = null as ReturnType<typeof spawn> | null;

    if (runCommand.promptInput === 'stdin-file') {
      promptFile = isolatedTextOnly
        ? path.join(isolatedTextOnly.cwd, '.prompt')
        : `/tmp/agent_prompt_${process.env.TASK_ID || 'unknown'}.txt`;
      writePrivateFileSync(
        promptFile,
        isolatedTextOnly ? prompt : withExecutionModePrompt(prompt),
      );
      promptFd = fs.openSync(promptFile, 'r');
    }

    child = spawn(runCommand.command, runCommand.args, {
      cwd: isolatedTextOnly?.cwd || WORKSPACE,
      env: runCommand.env,
      detached: true,
      stdio: [runCommand.promptInput === 'stdin' ? 'pipe' : (promptFd ?? 'ignore'), 'pipe', 'pipe'],
    });

    // Validate stdio BEFORE registering the PID or arming any watchdog timers.
    // With stdio:'pipe' this should never be null, but guard defensively: if
    // either stream is absent the child is already running detached and we must
    // kill its process group, clean up the fd, and bail without leaking anything.
    if (!child.stdout || !child.stderr) {
      const spawnedPid = child.pid;
      if (typeof spawnedPid === 'number') {
        try { process.kill(-spawnedPid, 'SIGKILL'); } catch { /* already gone */ }
      }
      if (promptFd !== undefined) fs.closeSync(promptFd);
      sendLog(ws, '[runtime] spawn returned no stdio streams — aborting run\n');
      resolve(1);
      return;
    }

    recordLlmCallStart();
    if (typeof child.pid === 'number') {
      registerChildPid(child.pid);
    }
    if (promptFd !== undefined) fs.closeSync(promptFd);
    if (runCommand.promptInput === 'stdin') {
      try {
        child.stdin?.end(isolatedTextOnly ? prompt : withExecutionModePrompt(prompt));
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
    const setForceKillTimer = (callback: () => void): void => {
      if (forceKillTimer) clearTimeout(forceKillTimer);
      forceKillTimer = setTimeout(() => {
        // Mark that the agent itself is issuing this SIGKILL so the close
        // handler does not misclassify it as a cgroup/external OOM.
        flags.agentKillIssued = true;
        callback();
      }, PROCESS_KILL_GRACE_MS);
    };
    const enforceOutputLimit = () => {
      const outputLimit = isolatedTextOnly?.maxOutputBytes ?? MAX_OUTPUT_BYTES;
      if (Buffer.byteLength(allOutput, 'utf8') < outputLimit || flags.outputLimited || allOutputTailMode || !processGroupStillOwned()) return;
      // Structured stream modes wrap every token in a JSON event envelope, so
      // raw bytes overstate model output by 20-100x — passing the cap
      // mid-task is normal for a fast provider, not runaway. Keep the run
      // alive and degrade to bounded-tail buffering; runaway remains bounded
      // by the stall/no-progress watchdogs and the run timeout. Isolated
      // receipt runs keep the hard kill: their tiny caps ARE the contract.
      const structuredStream = !isolatedTextOnly
        && (runCommand.outputMode === 'claude-stream-json' || runCommand.outputMode === 'codex-jsonl');
      if (structuredStream) {
        allOutputTailMode = true;
        sendLog(ws, `\n[OUTPUT_LIMIT] ${runCommand.displayName} raw stream passed ${outputLimit} bytes — structured mode, run continues; buffering tail only.\n`);
        return;
      }
      flags.outputLimited = true;
      sendLog(ws, `\n[OUTPUT_LIMIT] ${runCommand.displayName} exceeded ${outputLimit} output bytes; terminating text-only run.\n`);
      const pid = originalPid;
      if (pid !== undefined) {
        try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
        setForceKillTimer(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
        });
      }
    };
    const resetStallWatchdog = () => {
      if (stallWatchdog) clearTimeout(stallWatchdog);
      stallWatchdog = setTimeout(() => {
        if (!processGroupStillOwned()) return;
        sendLog(ws, `\n[runtime] stream-stall: no stdout for >${CLAUDE_STREAM_STALL_MS / 1000}s — sending SIGTERM so wrapper can proceed to commit/push.\n`);
        const pid = originalPid;
        if (pid !== undefined) {
          try { process.kill(-pid, 'SIGTERM'); } catch { /* ignore */ }
          setForceKillTimer(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          });
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
        setForceKillTimer(() => {
          if (!processGroupStillOwned()) return;
          try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
        });
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
          setForceKillTimer(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          });
        }
      }
    }, 60_000);

    // LLM-no-progress watchdog: a silent provider hang (Anthropic 429/529 the
    // Claude Code SDK absorbs via internal retry) emits non-content stdout — so
    // the raw-stdout stall watchdog keeps resetting — while producing no
    // assistant/result content events and 0 output tokens. Detect the missing
    // content-event progress (when no tool is legitimately in flight) and report
    // it as a rate-limit so the orchestrator holds/reroutes instead of letting
    // the call ride to the wall-clock backstop → AGENT_TIMEOUT → retry-
    // budget burn → terminal failure of a healthy task (incident 2026-05-29).
    let llmProgressWatchdog: NodeJS.Timeout | null = partialMessageProgressAvailable
      ? setInterval(() => {
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
      // alive, while one that goes fully dark is killed at the 30/45-min stall
      // budget as a correctly-classified ClaudeTimeoutError, never a phantom
      // rate_limit).
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
          setForceKillTimer(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          });
        }
      }
      }, 60_000)
      : null;

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
          setForceKillTimer(() => {
            if (!processGroupStillOwned()) return;
            try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
          });
        }
      }, 60_000);
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
            lastStreamEventType = objType || lastStreamEventType;
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
              if (!isNested) {
                lastStreamEventAt = Date.now();
                modelOutputSeen = true;  // run report: model is actively generating
              }
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
              if (!isNested) {
                lastStreamEventAt = Date.now();
                modelOutputSeen = true;  // run report: model produced a turn
              }
              if (obj.message?.content) {
                for (const block of obj.message.content) {
                  if (block.type === 'text') {
                    sendLog(ws, (block.text ?? '') + '\n');
                    statusRef.value = (block.text ?? '').slice(0, 100);
                    if (!isNested && block.text) observer?.onAssistantText?.(block.text);
                  } else if (block.type === 'tool_use') {
                    sendLog(ws, `[tool: ${strVal(block.name)}] ${JSON.stringify(block.input).slice(0, 200)}\n`);
                    statusRef.value = `Using tool: ${strVal(block.name)}`;
                    recordToolUse(strVal(block.name), block.input);
                    // A nested tool_use belongs to a sub-agent under an already
                    // in-flight workflow tool — it refreshes progress (above) but
                    // does not replace the parent's in-flight tracking.
                    if (!isNested) {
                      observer?.onToolUse?.(strVal(block.name), block.input);
                      toolInFlight = true;
                      workflowToolInFlight = isWorkflowToolName(strVal(block.name));
                      lastToolUseAt = Date.now();
                      lastToolProgressAt = Date.now();
                      statusRef.toolStartedAt = Date.now();
                      statusRef.lastProgressAt = Date.now();
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
              if (!isNested) {
                lastStreamEventAt = Date.now();  // terminal model event — reset no-progress watchdog
                modelOutputSeen = true;
                resultEventSeen = true;  // run report: the run completed its model turn
              }
              lastResultText = (obj as { result?: string }).result ?? '';
              sendLog(ws, `\n--- Result ---\n${lastResultText}\n`);
              const usage = (obj as { result?: string; usage?: Record<string, unknown> }).usage;
              if (!isNested && usage && typeof usage === 'object') {
                // Provider-reported path (Component B): capture raw provider usage
                // BEFORE normalization — separate accumulator independent from
                // recordLlmTokens so the backend can cross-check the two paths.
                // extractProviderReportedUsage handles both Anthropic-shaped and
                // OpenAI/DeepSeek-shaped usage blocks, so every provider that
                // emits a usage object lands in provider_reported_usage.
                const providerUsage = extractProviderReportedUsage(usage);
                recordProviderReportedUsage(
                  providerUsage?.inputTokens ?? 0,
                  providerUsage?.outputTokens ?? 0,
                  providerUsage?.cacheCreationTokens ?? 0,
                  providerUsage?.cacheReadTokens ?? 0,
                );

                const normalizedUsage = normalizeProviderTokenUsage(usage);
                if (normalizedUsage) {
                  recordLlmTokens(
                    normalizedUsage.inputTokens,
                    normalizedUsage.outputTokens,
                    normalizedUsage.cacheCreationTokens,
                    normalizedUsage.cacheReadTokens,
                  );
                }
              }
              // num_turns is a top-level field on the Claude Code stream-json result
              // event (sibling of `usage`, not inside it).
              const numTurns = Number((obj as { num_turns?: unknown }).num_turns ?? 0);
              if (!isNested) recordNumTurns(numTurns);
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
                    setForceKillTimer(() => {
                      if (!processGroupStillOwned()) return;
                      try { process.kill(-pid, 'SIGKILL'); } catch { /* ignore */ }
                    });
                  }
                }, CLAUDE_RESULT_GRACE_MS);
              }
            } else if (objType === 'error') {
              const errObj = (obj as { error?: { message?: string; type?: string; code?: string } }).error;
              const errLine = `[claude-stream error] ${sanitize(errObj?.message ?? '')}\n`;
              sendLog(ws, errLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, errLine, STDERR_TAIL_BYTES);
              // Run report: a structured provider error event — the truthful
              // "the provider rejected/aborted this call" signal.
              recordProviderError(
                'claude-stream',
                errObj?.message ?? '(no message)',
                typeof errObj?.code === 'string' ? errObj.code : (typeof errObj?.type === 'string' ? errObj.type : undefined),
              );
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
                statusRef.toolStartedAt = undefined;
                statusRef.lastProgressAt = undefined;
                if (enableCheckWatchdog && lastCheckOutputOffset >= 0) {
                  const slice = allOutput.slice(lastCheckOutputOffset);
                  recordCheckOutcome(!/\[check\].*FAILED/.test(slice));
                  lastCheckOutputOffset = -1;
                }
                // Accumulate tool-result content bytes for per-session attribution.
                const content = (obj as { message?: { content?: unknown[] } }).message?.content;
                if (Array.isArray(content)) {
                  let byteSum = 0;
                  for (const block of content) {
                    byteSum += Buffer.byteLength(JSON.stringify(block));
                  }
                  if (byteSum > 0) recordToolResultBytes(byteSum);
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
            lastStreamEventType = eventType || lastStreamEventType;
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
              modelOutputSeen = true;
              resultEventSeen = true;  // run report: codex terminal turn event
              const usage = obj.usage as Record<string, unknown> | undefined;
              if (usage && typeof usage === 'object') {
                // Provider-reported path (Component B): OpenAI/Codex reports
                // usage as prompt_tokens/completion_tokens (+ cached sub-counts).
                // extractProviderReportedUsage maps it into the persisted four-field
                // shape so openai tasks populate provider_reported_usage too.
                const providerUsage = extractProviderReportedUsage(usage);
                recordProviderReportedUsage(
                  providerUsage?.inputTokens ?? 0,
                  providerUsage?.outputTokens ?? 0,
                  providerUsage?.cacheCreationTokens ?? 0,
                  providerUsage?.cacheReadTokens ?? 0,
                );
                const normalizedUsage = normalizeProviderTokenUsage(usage);
                if (normalizedUsage) {
                  recordLlmTokens(
                    normalizedUsage.inputTokens,
                    normalizedUsage.outputTokens,
                    normalizedUsage.cacheCreationTokens,
                    normalizedUsage.cacheReadTokens,
                  );
                }
              }
              // Codex CLI event shape is provider-specific: num_turns may be inside
              // `usage` or at the top level depending on the CLI version. Check both.
              const codexUsageObj = usage && typeof usage === 'object' ? usage : null;
              const numTurns = Number(
                (codexUsageObj?.num_turns) ?? (obj as { num_turns?: unknown }).num_turns ?? 0,
              );
              // Codex uses a one-shot turn model with no nested sub-agent stream (see
              // comment above), so every turn.completed is a top-level event — no
              // isNested guard needed unlike the Claude provider.
              recordNumTurns(numTurns);
            } else if (eventType === 'turn.failed') {
              const errEnvelope = obj.error as { message?: string; code?: string } | undefined;
              const err = errEnvelope?.message ?? '';
              const oneLine = `[codex] turn.failed: ${sanitize(err)}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
              recordProviderError(
                'codex-turn-failed',
                err || '(no message)',
                typeof errEnvelope?.code === 'string' ? errEnvelope.code : undefined,
              );
            } else if (eventType === 'error') {
              const msg = typeof obj.message === 'string' ? obj.message : '';
              const oneLine = `[codex] error: ${sanitize(msg)}\n`;
              sendLog(ws, oneLine);
              stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
              recordProviderError('codex-error', msg || '(no message)');
            } else if (
              eventType === 'item.started'
              || eventType === 'item.updated'
              || eventType === 'item.completed'
            ) {
              lastStreamEventAt = Date.now();  // model producing — reset no-progress watchdog
              modelOutputSeen = true;  // run report: codex item events are model work
              const item = obj.item as Record<string, unknown> | undefined;
              const itemType = item && typeof item.type === 'string' ? item.type : '';
              const preview = _codexItemPreview(item);
              const toolName = _codexToolNameForItem(item);
              if (toolName) {
                if (eventType === 'item.started' || eventType === 'item.updated') {
                  sendLog(ws, `[tool: ${toolName}] ${preview.slice(0, 200)}\n`);
                  statusRef.value = `Using tool: ${toolName}`;
                  recordToolUse(toolName, item ?? {});
                  if (eventType === 'item.started') observer?.onToolUse?.(toolName, item ?? {});
                  toolInFlight = true;
                  workflowToolInFlight = isWorkflowToolName(toolName);
                  lastToolUseAt = Date.now();
                  lastToolProgressAt = Date.now();
                  statusRef.toolStartedAt = Date.now();
                  statusRef.lastProgressAt = Date.now();
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
                  statusRef.toolStartedAt = undefined;
                  statusRef.lastProgressAt = undefined;
                  const status = item && typeof item.status === 'string' ? item.status : '';
                  sendLog(ws, `[tool: ${toolName}] ${status}\n`);
                }
              } else if (itemType === 'agent_message') {
                if (eventType === 'item.completed' || eventType === 'item.updated') {
                  sendLog(ws, preview + '\n');
                  if (preview) statusRef.value = preview.slice(0, 100);
                  if (eventType === 'item.completed') {
                    lastResultText = (item != null && typeof item.text === 'string') ? item.text : preview;
                    if (preview) observer?.onAssistantText?.(preview);
                  }
                }
              } else if (itemType === 'reasoning') {
                if (eventType === 'item.completed') {
                  statusRef.value = `Reasoning: ${preview.slice(0, 80)}`;
                }
              } else if (itemType === 'error') {
                const msg = item && typeof item.message === 'string' ? item.message : '';
                const oneLine = `[codex] item error: ${sanitize(msg)}\n`;
                sendLog(ws, oneLine);
                stderrBuffer = appendBoundedTail(stderrBuffer, oneLine, STDERR_TAIL_BYTES);
                recordProviderError('codex-item-error', msg || '(no message)');
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
      if (toolInFlight) {
        lastToolProgressAt = Date.now();
        statusRef.lastProgressAt = Date.now();
      }
      const text = data.toString();
      if (allOutputTailMode) {
        allOutputTail = appendBoundedTail(allOutputTail, text, OUTPUT_TAIL_AFTER_CAP_BYTES);
      } else {
        allOutput = appendWithinByteLimit(
          allOutput,
          text,
          isolatedTextOnly?.maxOutputBytes ?? MAX_OUTPUT_BYTES,
        );
        enforceOutputLimit();
      }
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
          modelOutputSeen = true;  // run report: text-mode providers have no event schema
          recordToolUse('model_output', trimmed.slice(0, 200));
          observer?.onAssistantText?.(text);
        }
      }
    });

    child.stderr.on('data', (data: Buffer) => {
      const text = sanitize(data.toString());
      if (allOutputTailMode) {
        allOutputTail = appendBoundedTail(allOutputTail, text, OUTPUT_TAIL_AFTER_CAP_BYTES);
      } else {
        allOutput = appendWithinByteLimit(
          allOutput,
          text,
          isolatedTextOnly?.maxOutputBytes ?? MAX_OUTPUT_BYTES,
        );
        enforceOutputLimit();
      }
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
      if (allOutputTailMode) {
        // Re-join so post-exit consumers (rate-limit matching, marker regex
        // scans) see the true end of the stream, not the pre-cap head. Reset
        // the mode so a second resolve path cannot double-append.
        allOutput = `${allOutput}\n[OUTPUT_LIMIT] middle of raw stream omitted (structured-mode byte cap); tail follows:\n${allOutputTail}`;
        allOutputTailMode = false;
        allOutputTail = '';
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
      closeSignalSeen = closeSignal;
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
      if (closeSignal === 'SIGKILL' && !flags.timedOut && !flags.agentKillIssued) {
        flags.providerKilledByOOM = true;
        const oomLine = `EOOM: ${runCommand.displayName} process received external SIGKILL before agent timeout; likely cgroup OOM\n`;
        stderrBuffer = appendBoundedTail(stderrBuffer, oomLine, STDERR_TAIL_BYTES);
        allOutput = appendWithinByteLimit(allOutput, oomLine, MAX_OUTPUT_BYTES);
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
    recordRunErrorReport(exitCode, 'timeout');
    throw new ClaudeTimeoutError(`${runner.displayName} exceeded ${runCommand.timeoutMs / 60000}-minute time limit`);
  }
  if (flags.outputLimited) {
    recordRunErrorReport(exitCode, 'output_limit');
    throw new ClaudeTimeoutError(`${runner.displayName} exceeded the ${isolatedTextOnly?.maxOutputBytes ?? MAX_OUTPUT_BYTES}-byte output limit`);
  }
  if (flags.stalledMidStream) {
    recordRunErrorReport(exitCode, 'tool_stall');
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
      recordRunErrorReport(exitCode, 'rate_limit');
      const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
      throw new RateLimitError(`${runner.displayName} produced no model output and hit a confirmed rate limit — ${snippet}`);
    }
    recordRunErrorReport(exitCode, 'no_progress_hang');
    throw new NoProgressTimeoutError(`${runner.displayName} produced no model output (no stream deltas) for >${LLM_NO_PROGRESS_STALL_MS / 60000} min — transient provider hang, no confirmed rate-limit message`);
  }
  if (flags.providerKilledByOOM) {
    recordRunErrorReport(exitCode, 'oom');
    const err = new ClaudeTimeoutError(`EOOM: ${runner.displayName} received external SIGKILL before agent timeout; likely cgroup OOM`);
    (err as Error & { code?: string }).code = 'EOOM';
    throw err;
  }

  // Checked before auth-failure/rate-limit: a CLI that died because its
  // materialized config file is gone is a VM fault, and its sparse output must
  // not fall through to the generic non-zero-exit path (whose init-banner-only
  // tail the orchestrator reclassifies as usage exhaustion).
  if (exitCode !== 0 && matchesVmConfigMissing(combined)) {
    recordRunErrorReport(exitCode, 'vm_config_missing');
    const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
    throw new VmConfigMissingError(`${runner.displayName} provider config file missing on VM (boot/retarget materialization lost) — ${snippet}`);
  }
  if (sawConfirmedRateLimit) {
    recordRunErrorReport(exitCode, 'rate_limit');
    const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
    throw new RateLimitError(`${runner.displayName} API usage exhausted — ${snippet}`);
  }
  if (exitCode !== 0 && matchesAuthFailure(combined, runner.provider)) {
    recordRunErrorReport(exitCode, 'auth_failure');
    const snippet = (stderrBuffer || allOutput.slice(-500)).slice(0, 500);
    throw new AuthFailureError(`${runner.displayName} authentication failed — ${snippet}`);
  }

  // No agent-side matcher fired — record the unclassified run report (the
  // backend's structured-report classifier consumes the run-shape fields).
  recordRunErrorReport(exitCode);

  const stderrTail = stderrBuffer.slice(-500);
  return {
    exitCode,
    allOutput,
    lastResultText: lastResultText || allOutput.trim(),
    codexTurnCompleted,
    stderrTail,
  };
}
