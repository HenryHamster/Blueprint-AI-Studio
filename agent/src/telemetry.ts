import { createHash } from 'crypto';

// ── Error fingerprinting ──────────────────────────────────────────────────────

/** Normalized error fingerprint for detecting identical consecutive failures. */
export function errorFingerprint(msg: string): string {
  return msg.replace(/\s+/g, ' ').trim().slice(0, 2000);
}

// ── Prompt fingerprint ───────────────────────────────────────────────────────
// SHA-256 of the static prompt content (guidance blocks + task-type sections),
// truncated to 12 hex chars. Handlers set this before calling runClaudeCode so
// heartbeats and task records can correlate behavioral regressions to prompt edits.
// Task-specific noise (task IDs, file paths, payload text) must NOT be included.

/** Compute a stable 12-hex SHA-256 fingerprint of static prompt content. */
export function computePromptFingerprint(staticContent: string): string {
  return createHash('sha256').update(staticContent).digest('hex').slice(0, 12);
}

let currentPromptFingerprint: string | null = null;

/** Register the fingerprint for the current task's prompt. Call before runClaudeCode. */
export function setCurrentPromptFingerprint(fp: string | null): void {
  currentPromptFingerprint = fp;
}

export function getCurrentPromptFingerprint(): string | null {
  return currentPromptFingerprint;
}

// ── Per-task LLM telemetry (efficiency direction d-91d8dede) ─────────────────
// Cumulative counters posted on every heartbeat / terminal status. Bumped by
// the LLM CLI runner (one increment per spawn) and by the stream-json result
// parser (token totals). Reset between roles so warm-VM continuity does not
// double-count prior roles' usage. Tokens accumulate across all turns within
// a single CLI invocation; llm_call_count counts CLI invocations.
let _llmCallCount = 0;
let _totalInputTokens = 0;
let _totalOutputTokens = 0;

/** Increment the per-task LLM call counter (one per CLI invocation). */
export function recordLlmCallStart(): void {
  _llmCallCount += 1;
}

/** Add the input/output token totals reported by a single CLI result event. */
export function recordLlmTokens(inputTokens: number, outputTokens: number): void {
  if (Number.isFinite(inputTokens) && inputTokens > 0) {
    _totalInputTokens += Math.floor(inputTokens);
  }
  if (Number.isFinite(outputTokens) && outputTokens > 0) {
    _totalOutputTokens += Math.floor(outputTokens);
  }
}

/** Snapshot the current cumulative LLM telemetry for heartbeat / status posts. */
export function getLlmTelemetry(): {
  llm_call_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
} {
  return {
    llm_call_count: _llmCallCount,
    total_input_tokens: _totalInputTokens,
    total_output_tokens: _totalOutputTokens,
  };
}

/** Reset between roles so per-task counters don't bleed across warm-VM continuity. */
export function resetLlmTelemetry(): void {
  _llmCallCount = 0;
  _totalInputTokens = 0;
  _totalOutputTokens = 0;
}

/** Estimate token count from character count (4 chars ≈ 1 token).
 *  Used for providers that run in plain-text output mode (e.g. Gemini CLI with
 *  -o text) and don't emit structured token-usage blocks in their output. */
export function estimateTokensFromChars(chars: number): number {
  return Math.ceil(chars / 4);
}
