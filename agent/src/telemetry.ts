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
let _totalCacheCreationInputTokens = 0; // @per-role
let _totalCacheReadInputTokens = 0; // @per-role

// ── Provider-reported usage accumulator (Component B, independent cost reconciliation) ─
// SEPARATE from the api_call_summary counters above. Sums the RAW provider-reported
// usage fields from Anthropic messages.create() response.usage objects (input_tokens,
// output_tokens, cache_creation_input_tokens, cache_read_input_tokens) through an
// INDEPENDENT code path. The backend cross-checks these against the api_call_summary
// values to detect accumulation bugs, double-counting, or dropped-call accounting.
// This path is only populated from actual provider API responses, never from
// character-count estimation (text-output fallback), so it provides a genuine
// ground-truth comparison for Anthropic provider tasks.
// @per-role — reset between roles.
let _providerInputTokens = 0;
let _providerOutputTokens = 0;
let _providerCacheCreationInputTokens = 0;
let _providerCacheReadInputTokens = 0;
let _viewportScreenshotForwardedCount = 0; // @per-role
let _totalNumTurns = 0; // @per-role
let _totalToolResultBytes = 0; // @per-role
let _promptSizeBytes: number | null = null; // @per-role; set once per prompt assembly
let _promptSectionCount: number | null = null; // @per-role
let _promptSectionSizes: Array<{ name: string; bytes: number }> | null = null; // @per-role

/** Increment the per-task LLM call counter (one per CLI invocation). */
export function recordLlmCallStart(): void {
  _llmCallCount += 1;
}

/** Add the input/output token totals reported by a single CLI result event.
 *
 *  `inputTokens` is the INCLUSIVE sum of provider-reported input-token
 *  classes (uncached + cache-creation + cache-read) so total_input_tokens stays
 *  backward-compatible. The optional `cacheCreationTokens` / `cacheReadTokens`
 *  break out the two cache classes into their own counters so the backend can
 *  price each input class at its true rate. Callers without cache data (e.g.
 *  text-output-mode estimation) omit them and leave the cache counters at 0. */
export function recordLlmTokens(
  inputTokens: number,
  outputTokens: number,
  cacheCreationTokens = 0,
  cacheReadTokens = 0,
): void {
  if (Number.isFinite(inputTokens) && inputTokens > 0) {
    _totalInputTokens += Math.floor(inputTokens);
  }
  if (Number.isFinite(outputTokens) && outputTokens > 0) {
    _totalOutputTokens += Math.floor(outputTokens);
  }
  if (Number.isFinite(cacheCreationTokens) && cacheCreationTokens > 0) {
    _totalCacheCreationInputTokens += Math.floor(cacheCreationTokens);
  }
  if (Number.isFinite(cacheReadTokens) && cacheReadTokens > 0) {
    _totalCacheReadInputTokens += Math.floor(cacheReadTokens);
  }
}

/** Record raw provider-reported token usage from an Anthropic messages.create()
 *  response.usage object. Uses a SEPARATE accumulator from recordLlmTokens so the
 *  backend can cross-check the two paths and detect accumulation bugs.
 *
 *  Called only from sites that receive actual provider API response.usage objects
 *  (not text-output fallback estimation). Values are the RAW provider-reported
 *  fields as received, NOT the inclusive-normalized totals:
 *  - `inputTokens` = raw response.usage.input_tokens (uncached input only)
 *  - `outputTokens` = raw response.usage.output_tokens
 *  - `cacheCreationTokens` = raw response.usage.cache_creation_input_tokens
 *  - `cacheReadTokens` = raw response.usage.cache_read_input_tokens */
export function recordProviderReportedUsage(
  inputTokens: number,
  outputTokens: number,
  cacheCreationTokens = 0,
  cacheReadTokens = 0,
): void {
  if (Number.isFinite(inputTokens) && inputTokens > 0) {
    _providerInputTokens += Math.floor(inputTokens);
  }
  if (Number.isFinite(outputTokens) && outputTokens > 0) {
    _providerOutputTokens += Math.floor(outputTokens);
  }
  if (Number.isFinite(cacheCreationTokens) && cacheCreationTokens > 0) {
    _providerCacheCreationInputTokens += Math.floor(cacheCreationTokens);
  }
  if (Number.isFinite(cacheReadTokens) && cacheReadTokens > 0) {
    _providerCacheReadInputTokens += Math.floor(cacheReadTokens);
  }
}

/** Record that viewport screenshots were downloaded and forwarded into an agent prompt. */
export function recordViewportScreenshotDownload(count: number): void {
  if (count > 0) {
    _viewportScreenshotForwardedCount += count;
  }
}

/** Add to the per-task turn counter from a single CLI invocation result event.
 *  Turns are user→assistant exchanges; distinct from llm_call_count (API calls).
 *  Accumulates only top-level result events (not nested sub-agent results). */
export function recordNumTurns(numTurns: number): void {
  if (Number.isFinite(numTurns) && numTurns > 0) {
    _totalNumTurns += Math.floor(numTurns);
  }
}

/** Add to the per-task tool-result byte counter from top-level user events.
 *  Bytes are the JSON-serialized size of each tool_result content block.
 *  Accumulated only for top-level events (not nested sub-agent tool results). */
export function recordToolResultBytes(bytes: number): void {
  if (Number.isFinite(bytes) && bytes > 0) {
    _totalToolResultBytes += Math.floor(bytes);
  }
}

/** Store prompt-size telemetry so it is included on every subsequent heartbeat.
 *  Called once after prompt assembly in research/plan handlers. */
export function setPromptSizeTelemetry(
  sizeBytes: number,
  sectionCount: number,
  sectionSizes: Array<{ name: string; bytes: number }>,
): void {
  _promptSizeBytes = sizeBytes;
  _promptSectionCount = sectionCount;
  _promptSectionSizes = sectionSizes;
}

/** Build prompt-size telemetry from the assembled prompt and its named sections.
 *  Sorts sections descending by byte size and caps at the top 10 for heartbeat
 *  payload size. Returns an object suitable for destructuring into
 *  setPromptSizeTelemetry. */
export function buildPromptSizeTelemetry(
  fullPrompt: string,
  sections: Array<[string, string]>,
): {
  prompt_size_bytes: number;
  prompt_section_count: number;
  prompt_section_sizes: Array<{ name: string; bytes: number }>;
} {
  const sized = sections
    .map(([name, text]) => ({ name, bytes: Buffer.byteLength(text, 'utf8') }))
    .filter(s => s.bytes > 0)
    .sort((a, b) => b.bytes - a.bytes);
  return {
    prompt_size_bytes: Buffer.byteLength(fullPrompt, 'utf8'),
    prompt_section_count: sized.length,
    prompt_section_sizes: sized.slice(0, 10),
  };
}

/** Snapshot the current cumulative LLM telemetry for heartbeat / status posts. */
export function getLlmTelemetry(): {
  llm_call_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_creation_input_tokens: number;
  total_cache_read_input_tokens: number;
  viewport_screenshot_forwarded_count: number;
  num_turns: number;
  total_tool_result_bytes: number;
  prompt_size_bytes: number | null;
  prompt_section_count: number | null;
  prompt_section_sizes: Array<{ name: string; bytes: number }> | null;
  /** Provider-reported usage from response.usage objects (Component B).
   *  Nested object so the backend can structure-parse it independently from the
   *  flat-spread api_call_summary fields. Absent when no LLM calls were made
   *  (all zeroes) or when only character-estimated tokens exist. */
  provider_reported_usage?: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
  } | null;
} {
  const hasProviderReportedUsage =
    _providerInputTokens > 0
    || _providerOutputTokens > 0
    || _providerCacheCreationInputTokens > 0
    || _providerCacheReadInputTokens > 0;
  return {
    llm_call_count: _llmCallCount,
    total_input_tokens: _totalInputTokens,
    total_output_tokens: _totalOutputTokens,
    total_cache_creation_input_tokens: _totalCacheCreationInputTokens,
    total_cache_read_input_tokens: _totalCacheReadInputTokens,
    viewport_screenshot_forwarded_count: _viewportScreenshotForwardedCount,
    num_turns: _totalNumTurns,
    total_tool_result_bytes: _totalToolResultBytes,
    prompt_size_bytes: _promptSizeBytes,
    prompt_section_count: _promptSectionCount,
    prompt_section_sizes: _promptSectionSizes,
    provider_reported_usage: hasProviderReportedUsage
      ? {
          input_tokens: _providerInputTokens,
          output_tokens: _providerOutputTokens,
          cache_creation_input_tokens: _providerCacheCreationInputTokens,
          cache_read_input_tokens: _providerCacheReadInputTokens,
        }
      : null,
  };
}

/** Reset between roles so per-task counters don't bleed across warm-VM continuity. */
export function resetLlmTelemetry(): void {
  _llmCallCount = 0;
  _totalInputTokens = 0;
  _totalOutputTokens = 0;
  _totalCacheCreationInputTokens = 0;
  _totalCacheReadInputTokens = 0;
  _providerInputTokens = 0;
  _providerOutputTokens = 0;
  _providerCacheCreationInputTokens = 0;
  _providerCacheReadInputTokens = 0;
  _viewportScreenshotForwardedCount = 0;
  _totalNumTurns = 0;
  _totalToolResultBytes = 0;
  _promptSizeBytes = null;
  _promptSectionCount = null;
  _promptSectionSizes = null;
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
  | 'security_audit'
  | 'self_audit'
  | 'release_audit'
  | 'audit_challenge'
  | 'merge_grade'
  | 'research_spawn_adversarial_challenge'
  | 'design'
  | 'research_plan'
  | 'web_research'
  | 'specialized_findings'
  | 'specialized_result'
  | 'thinker'
  | 'review_verdict'
  | 'panel_review_lens'
  | 'strategic'
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
// Cross-product: schema → reason → count. Enables localizing a miss-reason regression
// to a specific handler (schema label) rather than only the global reason bucket.
let _formatMissBySchemaAndReason: Partial<Record<SchemaLabel, Record<string, number>>> = {}; // @per-role
let _formatRepairAttemptsBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairSuccessesBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairInfraFailuresBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairInputTokensBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairOutputTokensBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role
let _formatRepairMissBySchema: Partial<Record<SchemaLabel, number>> = {}; // @per-role

/** Record one unparseable final output, bucketed by the extraction verdict's
 *  reason class and, when provided, by the bounded schema label. Bumps the
 *  per-reason count and the format-error total. Also records the cross-product
 *  (schema × reason) when both dimensions are present. */
export function recordFormatMiss(reason: FormatMissReason, schemaLabel?: SchemaLabel): void {
  _formatMissByReason[reason] = (_formatMissByReason[reason] ?? 0) + 1;
  _formatErrorTotal += 1;
  if (schemaLabel !== undefined) {
    _formatMissBySchema[schemaLabel] = (_formatMissBySchema[schemaLabel] ?? 0) + 1;
    const reasonMap = _formatMissBySchemaAndReason[schemaLabel] ?? {};
    reasonMap[reason] = (reasonMap[reason] ?? 0) + 1;
    _formatMissBySchemaAndReason[schemaLabel] = reasonMap;
  }
}

/** Record one unparseable reopen output, bucketed by the extraction verdict's
 *  reason class and, when provided, by the bounded schema label. Distinct from
 *  recordFormatMiss (which records the first-attempt miss); this records the
 *  reopen's failure reason when self-repair still fails. */
export function recordFormatRepairMiss(reason: FormatMissReason, schemaLabel?: SchemaLabel): void {
  _formatRepairMissByReason[reason] = (_formatRepairMissByReason[reason] ?? 0) + 1;
  if (schemaLabel !== undefined) {
    _formatRepairMissBySchema[schemaLabel] = (_formatRepairMissBySchema[schemaLabel] ?? 0) + 1;
  }
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
  format_repair_miss_by_schema: Partial<Record<SchemaLabel, number>>;
  format_miss_by_schema_and_reason: Partial<Record<SchemaLabel, Record<string, number>>>;
} {
  const schemaAndReason: Partial<Record<SchemaLabel, Record<string, number>>> = {};
  for (const [schema, reasons] of Object.entries(_formatMissBySchemaAndReason)) {
    schemaAndReason[schema as SchemaLabel] = { ...reasons };
  }
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
    format_repair_miss_by_schema: { ..._formatRepairMissBySchema },
    format_miss_by_schema_and_reason: schemaAndReason,
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
  _formatRepairMissBySchema = {};
  _formatMissBySchemaAndReason = {};
}

/** Estimate token count from character count (4 chars ≈ 1 token).
 *  Used for providers that run in plain-text output mode (e.g. Gemini CLI with
 *  -o text) and don't emit structured token-usage blocks in their output. */
export function estimateTokensFromChars(chars: number): number {
  return Math.ceil(chars / 4);
}

// ── Per-resident-session turn latency telemetry ───────────────────────────────
// Rolling per-kind (operator / autonomous) latency accumulators. Included in
// every session heartbeat so the backend can persist a turn-latency summary on
// the session blob for operator-visible observability.

interface ResidentTurnKindStats {
  count: number;
  totalTurnMs: number;
  maxTurnMs: number;
  totalTtftMs: number;
  ttftCount: number;
  interruptedCount: number;
  totalToolCalls: number;
  totalToolCallDurationMs: number;
  interTokenCount: number;
  totalInterTokenMs: number;
  totalPickupLagMs: number;
  pickupLagCount: number;
}

const _emptyKindStats = (): ResidentTurnKindStats => ({
  count: 0, totalTurnMs: 0, maxTurnMs: 0, totalTtftMs: 0,
  ttftCount: 0, interruptedCount: 0, totalToolCalls: 0,
  totalToolCallDurationMs: 0, interTokenCount: 0, totalInterTokenMs: 0,
  totalPickupLagMs: 0, pickupLagCount: 0,
});

let _residentTurnStats: Record<'operator' | 'autonomous', ResidentTurnKindStats> = { // @per-role
  operator: _emptyKindStats(),
  autonomous: _emptyKindStats(),
};

export interface ResidentTurnLatencyRecord {
  kind: 'operator' | 'autonomous';
  turnMs: number;
  ttftMs: number | null;
  interrupted: boolean;
  toolCallCount: number;
  /** cumulative ms spent inside tool calls during this turn. */
  toolCallDurationMs: number;
  /** mean ms between consecutive text tokens after the first; null when <=1
   *  text token arrived. */
  meanInterTokenMs: number | null;
  /** ms from operator message created_at to when the agent picked it up and
   *  started processing; null for autonomous turns or when created_at is absent. */
  pickupLagMs?: number | null;
}

/** Record one completed resident turn's latency metrics. */
export function recordResidentTurnLatency(record: ResidentTurnLatencyRecord): void {
  const s = _residentTurnStats[record.kind];
  s.count += 1;
  if (Number.isFinite(record.turnMs) && record.turnMs >= 0) {
    s.totalTurnMs += record.turnMs;
    if (record.turnMs > s.maxTurnMs) s.maxTurnMs = record.turnMs;
  }
  if (record.ttftMs !== null && Number.isFinite(record.ttftMs) && record.ttftMs >= 0) {
    s.totalTtftMs += record.ttftMs;
    s.ttftCount += 1;
  }
  if (record.interrupted) s.interruptedCount += 1;
  if (Number.isFinite(record.toolCallCount) && record.toolCallCount > 0) {
    s.totalToolCalls += record.toolCallCount;
  }
  if (Number.isFinite(record.toolCallDurationMs) && record.toolCallDurationMs > 0) {
    s.totalToolCallDurationMs += record.toolCallDurationMs;
  }
  if (record.meanInterTokenMs !== null && Number.isFinite(record.meanInterTokenMs) && record.meanInterTokenMs >= 0) {
    s.totalInterTokenMs += record.meanInterTokenMs;
    s.interTokenCount += 1;
  }
  if (record.pickupLagMs != null && Number.isFinite(record.pickupLagMs) && record.pickupLagMs >= 0) {
    s.totalPickupLagMs += record.pickupLagMs;
    s.pickupLagCount += 1;
  }
}

/** Build a per-kind summary object from accumulated stats. Returns null when no turns have been recorded. */
export function getResidentTurnLatencyTelemetry(): {
  operator: { count: number; mean_turn_ms: number | null; max_turn_ms: number; mean_ttft_ms: number | null; interrupted_count: number; total_tool_calls: number; total_tool_call_duration_ms: number; mean_inter_token_ms: number | null; mean_pickup_lag_ms: number | null };
  autonomous: { count: number; mean_turn_ms: number | null; max_turn_ms: number; mean_ttft_ms: number | null; interrupted_count: number; total_tool_calls: number; total_tool_call_duration_ms: number; mean_inter_token_ms: number | null; mean_pickup_lag_ms: number | null };
} | null {
  const op = _residentTurnStats.operator;
  const au = _residentTurnStats.autonomous;
  if (op.count === 0 && au.count === 0) return null;
  const summarize = (s: ResidentTurnKindStats) => ({
    count: s.count,
    mean_turn_ms: s.count > 0 ? Math.round(s.totalTurnMs / s.count) : null,
    max_turn_ms: s.maxTurnMs,
    mean_ttft_ms: s.ttftCount > 0 ? Math.round(s.totalTtftMs / s.ttftCount) : null,
    interrupted_count: s.interruptedCount,
    total_tool_calls: s.totalToolCalls,
    total_tool_call_duration_ms: s.totalToolCallDurationMs,
    mean_inter_token_ms: s.interTokenCount > 0 ? Math.round(s.totalInterTokenMs / s.interTokenCount) : null,
    mean_pickup_lag_ms: s.pickupLagCount > 0 ? Math.round(s.totalPickupLagMs / s.pickupLagCount) : null,
  });
  return { operator: summarize(op), autonomous: summarize(au) };
}

/** Reset resident turn latency accumulators between roles. */
export function resetResidentTurnLatencyTelemetry(): void {
  _residentTurnStats = { operator: _emptyKindStats(), autonomous: _emptyKindStats() };
}

// ── Operator-decision capture telemetry ─────────────────────────────────────
// Marker coverage consumed by the backend; corrective reviews never rewrite it.

export interface OperatorDecisionCaptureStats {
  total: number;
  captured: number;
}

let _operatorDecisionCaptureStats: OperatorDecisionCaptureStats = { total: 0, captured: 0 }; // @per-role

/**
 * Record one completed operator turn. `captured` means the reply carried its
 * marker; `markerless` means it did not. Boolean input remains accepted for
 * source compatibility (true→captured, false→markerless).
 */
export function recordOperatorDecisionCapture(
  outcome: 'captured' | 'markerless' | boolean,
): void {
  _operatorDecisionCaptureStats.total += 1;
  if (outcome === 'captured' || outcome === true) {
    _operatorDecisionCaptureStats.captured += 1;
  }
}

/** Snapshot the current operator-decision capture telemetry for heartbeat / status
 *  posts. Field meanings intentionally match the backend contract: `total` is
 *  operator turns, and `captured` is turns ending with the marker. */
export function getOperatorDecisionCaptureTelemetry(): {
  total: number;
  captured: number;
  missed: number;
  capture_rate: number | null;
} | null {
  const { total, captured } = _operatorDecisionCaptureStats;
  if (total === 0) return null;
  return {
    total,
    captured,
    missed: total - captured,
    capture_rate: Math.round((captured / total) * 100),
  };
}

/** Reset operator-decision capture accumulators between roles. */
export function resetOperatorDecisionCaptureTelemetry(): void {
  _operatorDecisionCaptureStats = { total: 0, captured: 0 };
}

/** Restore the durable per-session counters after a resident process restart. */
export function restoreOperatorDecisionCaptureTelemetry(
  stats: OperatorDecisionCaptureStats | null | undefined,
): void {
  _operatorDecisionCaptureStats = {
    total: Number.isFinite(stats?.total)
      ? Math.max(0, Math.floor(stats?.total ?? 0)) : 0,
    captured: Number.isFinite(stats?.captured)
      ? Math.max(0, Math.min(
        Math.floor(stats?.captured ?? 0),
        Math.floor(stats?.total ?? 0),
      )) : 0,
  };
}
