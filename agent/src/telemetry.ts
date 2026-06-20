import { createHash } from 'crypto';
import type { JsonExtractionMissReason } from './json-extract.js';

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

let currentPromptFingerprint: string | null = null; // @per-role

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
let _llmCallCount = 0; // @per-role
let _totalInputTokens = 0; // @per-role
let _totalOutputTokens = 0; // @per-role
let _viewportScreenshotForwardedCount = 0; // @per-role

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

/** Record that viewport screenshots were downloaded and forwarded into an agent prompt. */
export function recordViewportScreenshotDownload(count: number): void {
  if (count > 0) {
    _viewportScreenshotForwardedCount += count;
  }
}

/** Snapshot the current cumulative LLM telemetry for heartbeat / status posts. */
export function getLlmTelemetry(): {
  llm_call_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  viewport_screenshot_forwarded_count: number;
} {
  return {
    llm_call_count: _llmCallCount,
    total_input_tokens: _totalInputTokens,
    total_output_tokens: _totalOutputTokens,
    viewport_screenshot_forwarded_count: _viewportScreenshotForwardedCount,
  };
}

/** Reset between roles so per-task counters don't bleed across warm-VM continuity. */
export function resetLlmTelemetry(): void {
  _llmCallCount = 0;
  _totalInputTokens = 0;
  _totalOutputTokens = 0;
  _viewportScreenshotForwardedCount = 0;
}

// ── Per-task structured-output format-compliance telemetry (equipping d-46468a41) ─
// Mirrors the LLM telemetry vertical above: cumulative per-run counters posted
// on every heartbeat and persisted next to api_call_summary as
// format_compliance_summary. Records straight from the extraction layer's own
// verdicts (json-extract.ts) — the verdict is GROUND TRUTH, never re-derived.
//
// A "format miss" is a FINAL structured output the extraction layer could not
// parse, bucketed by the layer's reason class. `malformed_json` is the bucket
// for a JsonExtractionError (a candidate parsed-but-threw), which carries no
// JsonExtractionMissReason. Handlers that self-repair an unparseable final
// output with a focused re-ask also record the attempt and whether it succeeded.

/** Reason class for an unparseable final output. The extraction layer's miss
 *  reasons plus `malformed_json` for a thrown JsonExtractionError. */
export type FormatMissReason =
  | JsonExtractionMissReason
  | 'malformed_json'
  | 'parsed_object_schema_mismatch'
  | 'parsed_array_invalid_value';

/** Bounded vocabulary for the output-schema dimension of format-compliance
 *  telemetry. Keeping this a literal union prevents unbounded telemetry keys
 *  from leaking in via task-prompt text. */
export type SchemaLabel =
  | 'memo'
  | 'audit'
  | 'audit_challenge'
  | 'research_spawn_adversarial_challenge'
  | 'design'
  | 'research_plan'
  | 'web_research'
  | 'specialized_findings'
  | 'specialized_result'
  | 'thinker'
  | 'review_verdict'
  | 'unknown';

let _formatMissByReason: Record<string, number> = {}; // @per-role
let _formatRepairMissByReason: Record<string, number> = {}; // @per-role
let _formatErrorTotal = 0; // @per-role
let _formatRepairAttempts = 0; // @per-role
let _formatRepairSuccesses = 0; // @per-role
let _formatRepairInfraFailures = 0; // @per-role
let _formatRepairInputTokens = 0; // @per-role
let _formatRepairOutputTokens = 0; // @per-role
let _formatMissBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairAttemptsBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairSuccessesBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairInfraFailuresBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairInputTokensBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairOutputTokensBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role

/** Record one unparseable final output, bucketed by the extraction verdict's
 *  reason class and, when provided, by the bounded schema label. Bumps the
 *  per-reason count and the format-error total. */
export function recordFormatMiss(reason: FormatMissReason, schemaLabel?: SchemaLabel): void {
  _formatMissByReason[reason] = (_formatMissByReason[reason] ?? 0) + 1;
  _formatErrorTotal += 1;
  if (schemaLabel !== undefined) {
    _formatMissBySchema[schemaLabel] = (_formatMissBySchema[schemaLabel] ?? 0) + 1;
  }
}

/** Record one unparseable reopen output, bucketed by the extraction verdict's
 *  reason class. Distinct from recordFormatMiss (which records the first-attempt
 *  miss); this records the reopen's failure reason when self-repair still fails. */
export function recordFormatRepairMiss(reason: FormatMissReason, _schemaLabel?: SchemaLabel): void {
  _formatRepairMissByReason[reason] = (_formatRepairMissByReason[reason] ?? 0) + 1;
}

/** Record that a handler attempted a self-repair re-ask after a format miss. */
export function recordFormatRepairAttempt(schemaLabel?: SchemaLabel): void {
  _formatRepairAttempts += 1;
  if (schemaLabel !== undefined) {
    _formatRepairAttemptsBySchema[schemaLabel] = (_formatRepairAttemptsBySchema[schemaLabel] ?? 0) + 1;
  }
}

/** Record that a self-repair re-ask then produced a parseable final output. */
export function recordFormatRepairSuccess(schemaLabel?: SchemaLabel): void {
  _formatRepairSuccesses += 1;
  if (schemaLabel !== undefined) {
    _formatRepairSuccessesBySchema[schemaLabel] = (_formatRepairSuccessesBySchema[schemaLabel] ?? 0) + 1;
  }
}

/** Record that a self-repair reopen run crashed (exitCode !== 0) — an
 *  infrastructure failure distinct from a clean-but-unparseable reopen. */
export function recordFormatRepairInfraFailure(schemaLabel?: SchemaLabel): void {
  _formatRepairInfraFailures += 1;
  if (schemaLabel !== undefined) {
    _formatRepairInfraFailuresBySchema[schemaLabel] = (_formatRepairInfraFailuresBySchema[schemaLabel] ?? 0) + 1;
  }
}

/** Record the LLM tokens consumed by a single self-repair reopen call.
 *  Called with the delta (after - before) from getLlmTelemetry() snapshots
 *  taken immediately before and after the repair runClaudeCode() call.
 *  Non-positive deltas (e.g. zero tokens from a crashed reopen) are ignored. */
export function recordFormatRepairTokens(
  inputTokensDelta: number,
  outputTokensDelta: number,
  schemaLabel?: SchemaLabel,
): void {
  if (Number.isFinite(inputTokensDelta) && inputTokensDelta > 0) {
    const floored = Math.floor(inputTokensDelta);
    _formatRepairInputTokens += floored;
    if (schemaLabel !== undefined) {
      _formatRepairInputTokensBySchema[schemaLabel] = (_formatRepairInputTokensBySchema[schemaLabel] ?? 0) + floored;
    }
  }
  if (Number.isFinite(outputTokensDelta) && outputTokensDelta > 0) {
    const floored = Math.floor(outputTokensDelta);
    _formatRepairOutputTokens += floored;
    if (schemaLabel !== undefined) {
      _formatRepairOutputTokensBySchema[schemaLabel] = (_formatRepairOutputTokensBySchema[schemaLabel] ?? 0) + floored;
    }
  }
}

/** Snapshot the cumulative format-compliance telemetry for heartbeat / status
 *  posts. Returns a plain dict (fresh copies of all per-reason and per-schema maps). */
export function getFormatComplianceTelemetry(): {
  format_miss_by_reason: Record<string, number>;
  format_repair_miss_by_reason: Record<string, number>;
  format_error_total: number;
  format_repair_attempts: number;
  format_repair_successes: number;
  format_repair_infra_failures: number;
  format_repair_input_tokens: number;
  format_repair_output_tokens: number;
  format_miss_by_schema: Partial<Record<SchemaLabel, number>>;
  format_repair_attempts_by_schema: Partial<Record<SchemaLabel, number>>;
  format_repair_successes_by_schema: Partial<Record<SchemaLabel, number>>;
  format_repair_infra_failures_by_schema: Partial<Record<SchemaLabel, number>>;
  format_repair_input_tokens_by_schema: Partial<Record<SchemaLabel, number>>;
  format_repair_output_tokens_by_schema: Partial<Record<SchemaLabel, number>>;
} {
  return {
    format_miss_by_reason: { ..._formatMissByReason },
    format_repair_miss_by_reason: { ..._formatRepairMissByReason },
    format_error_total: _formatErrorTotal,
    format_repair_attempts: _formatRepairAttempts,
    format_repair_successes: _formatRepairSuccesses,
    format_repair_infra_failures: _formatRepairInfraFailures,
    format_repair_input_tokens: _formatRepairInputTokens,
    format_repair_output_tokens: _formatRepairOutputTokens,
    format_miss_by_schema: { ..._formatMissBySchema },
    format_repair_attempts_by_schema: { ..._formatRepairAttemptsBySchema },
    format_repair_successes_by_schema: { ..._formatRepairSuccessesBySchema },
    format_repair_infra_failures_by_schema: { ..._formatRepairInfraFailuresBySchema },
    format_repair_input_tokens_by_schema: { ..._formatRepairInputTokensBySchema },
    format_repair_output_tokens_by_schema: { ..._formatRepairOutputTokensBySchema },
  };
}

/** Reset between roles so per-task counters don't bleed across warm-VM continuity. */
export function resetFormatComplianceTelemetry(): void {
  _formatMissByReason = {};
  _formatRepairMissByReason = {};
  _formatErrorTotal = 0;
  _formatRepairAttempts = 0;
  _formatRepairSuccesses = 0;
  _formatRepairInfraFailures = 0;
  _formatRepairInputTokens = 0;
  _formatRepairOutputTokens = 0;
  _formatMissBySchema = {};
  _formatRepairAttemptsBySchema = {};
  _formatRepairSuccessesBySchema = {};
  _formatRepairInfraFailuresBySchema = {};
  _formatRepairInputTokensBySchema = {};
  _formatRepairOutputTokensBySchema = {};
}

/** Estimate token count from character count (4 chars ≈ 1 token).
 *  Used for providers that run in plain-text output mode (e.g. Gemini CLI with
 *  -o text) and don't emit structured token-usage blocks in their output. */
export function estimateTokensFromChars(chars: number): number {
  return Math.ceil(chars / 4);
}
