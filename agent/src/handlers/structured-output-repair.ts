import type WebSocket from 'ws';
import { sendLog, runClaudeCode } from '../index.js';
import {
  extractJsonObjectDetailed, extractJsonArrayDetailed,
  describeJsonExtractionResult, JsonExtractionError,
} from '../json-extract.js';
import {
  recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess,
  recordFormatRepairInfraFailure, recordFormatRepairTokens, recordFormatRepairMiss,
  getLlmTelemetry,
  type FormatMissReason, type SchemaLabel,
} from '../telemetry.js';

// ── Shared bounded structured-output self-repair ─────────────────────────────
//
// memo.ts proved a local pattern: when a structured-output agent exits cleanly
// (exitCode===0) but its FINAL output is unparseable (a common transient format
// slip), reopen ONCE with a focused continuation prompt that re-states the
// schema and asks for ONLY the JSON, then re-extract. This module factors that
// sequence into a single generic helper so non-memo handlers (audit, design,
// web-research, research-plan, specialized, …) can adopt it without duplicating
// the extract → telemetry → bounded reopen → re-extract logic.
//
// The extraction layer's verdicts (json-extract.ts) are GROUND TRUTH: the miss
// reason recorded via recordFormatMiss comes straight from the verdict, never
// re-derived. `malformed_json` is the bucket for a thrown JsonExtractionError
// (a candidate parsed-but-threw), which carries no JsonExtractionMissReason.
//
// memo.ts is intentionally NOT rewired onto this helper here — its Codex
// agent_message recovery and failure-category classification are memo-specific
// and out of scope for this subtask. This module is the reusable primitive
// modeled on memo's reopen block; adoption lands in follow-up tasks.

/** A finished runClaudeCode result, narrowed to the fields the repair path uses. */
export interface RunResult {
  exitCode: number;
  allOutput: string;
  lastResultText: string;
  codexTurnCompleted?: boolean;
  stderrTail?: string;
}

export interface RepairStructuredOutputOptions {
  /** 'object' uses extractJsonObjectDetailed (required keys are own properties);
   *  'array' uses extractJsonArrayDetailed (required keys on element objects). */
  mode: 'object' | 'array';
  /** Required keys passed to the matching extractor as GROUND TRUTH. */
  requiredKeys: string[];
  /** The finished FIRST runClaudeCode result the caller already produced. */
  first: RunResult;
  /** Focused continuation prompt re-stating the caller's schema and asking for
   *  ONLY the JSON. Caller owns the full text (incl. any tool-guidance suffix);
   *  the helper passes it to runClaudeCode verbatim. */
  reopenPrompt: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  model?: string;
  effort?: string;
  disallowedTools?: string[];
  /** Optional status message set on statusRef before the reopen run. */
  retryStatus?: string;
  /** Optional bounded schema label for per-schema telemetry bucketing. When
   *  provided, format misses, repair attempts, and successes are also counted
   *  in the schema-dimensioned maps inside format-compliance telemetry. */
  schemaLabel?: SchemaLabel;
  /** Optional parsed-value validation hook. When provided, a parseable value
   *  that fails schema validation is treated like a format miss and reopened
   *  once, using the returned miss reason. */
  validateParsedValue?: (value: unknown) => ParsedValueValidationResult;
  /** Injectable runner for handlers with a narrow dependency facade. */
  run?: typeof runClaudeCode;
}

export type ParsedValueValidationResult =
  | { ok: true }
  | { ok: false; reason: FormatMissReason; detail: string };

export interface RepairStructuredOutputResult<T> {
  /** Parsed value (from the first attempt or the repaired retry), or null. */
  value: T | null;
  /** The first attempt's extraction miss reason (ground truth), or null when
   *  the first attempt parsed cleanly. Callers keep their own terminal-status
   *  semantics from this plus `retry`. */
  missReason: FormatMissReason | null;
  /** The reopen run's result, or null when no reopen was attempted (first
   *  attempt parsed, or exitCode !== 0). */
  retry: RunResult | null;
}

// Maximum characters of prior-output context to carry into the reopen prompt.
// Enough to cover a few paragraphs of reasoning without bloating the context window.
const MAX_PRIOR_OUTPUT_CHARS = 4000;

// Composes a re-ask prompt that leads with a reason-specific preamble so the
// agent knows exactly what was wrong, appends a bounded tail of the first-pass
// output as reformatting context, then appends the caller's schema restatement.
// Including the prior output means recovery is a reformat rather than a cold
// re-derivation (the first pass is often expensive multi-paragraph reasoning). When missReason is
// null (shouldn't happen on the repair path but defensive), the prior context
// is still included when non-empty so the caller's prompt gets full context.
function buildMissAwarePrompt(
  missReason: FormatMissReason | null,
  requiredKeys: string[],
  callerPrompt: string,
  priorOutput: string,
): string {
  let preamble: string;
  switch (missReason) {
    case 'no_json_start':
      preamble = 'Your previous response contained no JSON object or array — output ONLY the JSON.';
      break;
    case 'unbalanced_json':
    case 'malformed_json':
      preamble = 'Your previous response contained incomplete or malformed JSON — emit the COMPLETE valid JSON.';
      break;
    case 'missing_required_key':
    case 'no_matching_candidate':
    case 'parsed_object_missing_key':
    case 'parsed_array_missing_key':
      preamble = `Your previous response was missing required keys: ${requiredKeys.map(k => `"${k}"`).join(', ')} — include all required keys.`;
      break;
    case 'parsed_object_schema_mismatch':
      preamble = 'Your previous response parsed as JSON, but the top-level object shape did not match the required schema — fix the object shape and include all required keys.';
      break;
    case 'parsed_array_invalid_value':
      preamble = 'Your previous response parsed as JSON, but the array elements\' field values did not match the required schema — the shape did not match what was expected. Fix the field values to conform to the required format.';
      break;
    default:
      if (priorOutput) {
        const bounded = priorOutput.slice(-MAX_PRIOR_OUTPUT_CHARS);
        return `[Prior output]\n${bounded}\n[End prior output]\n\n${callerPrompt}`;
      }
      return callerPrompt;
  }
  if (priorOutput) {
    const bounded = priorOutput.slice(-MAX_PRIOR_OUTPUT_CHARS);
    return `${preamble}\n\n[Prior output]\n${bounded}\n[End prior output]\n\n${callerPrompt}`;
  }
  return `${preamble}\n\n${callerPrompt}`;
}

function buildSources(
  lastResultText: string,
  allOutput: string,
): Array<{ label: string; text: string }> {
  const sources: Array<{ label: string; text: string }> = [];
  if (lastResultText) sources.push({ label: 'lastResultText', text: lastResultText });
  if (allOutput) sources.push({ label: 'allOutput', text: allOutput });
  return sources;
}

// Extract a valid value from the first source that yields one. Returns the
// parsed value on first match (missReason null), or null with the extraction
// layer's reason for the FIRST schema-bearing source that failed — so the
// caller records format-compliance telemetry straight from the verdict.
function tryExtract(
  mode: 'object' | 'array',
  requiredKeys: string[],
  sources: Array<{ label: string; text: string }>,
  ws: WebSocket | null,
): { value: unknown; missReason: FormatMissReason | null } {
  let missReason: FormatMissReason | null = null;
  for (const src of sources) {
    try {
      const res = mode === 'object'
        ? extractJsonObjectDetailed(src.text, ...requiredKeys)
        : extractJsonArrayDetailed(src.text, ...requiredKeys);
      if (res.ok) return { value: res.value, missReason: null };
      if (missReason === null) missReason = res.reason;
      sendLog(ws, `[WARNING] Could not parse structured ${mode} from ${src.label}: ${describeJsonExtractionResult(res)}\n`);
    } catch (e) {
      // A thrown JsonExtractionError means a candidate matched the required
      // keys but JSON.parse threw — bucket it as malformed_json.
      if (missReason === null) missReason = 'malformed_json';
      sendLog(ws, `[WARNING] Structured ${mode} extraction from ${src.label} failed: ${e instanceof JsonExtractionError ? e.message : String(e)}\n`);
    }
  }
  return { value: null, missReason };
}

/**
 * Multi-source extraction with a single bounded self-repair reopen.
 *
 * 1. Extract from the finished first run, preferring lastResultText then
 *    allOutput, using the json-extract.ts verdicts as ground truth.
 * 2. On a clean exit (exitCode===0) where the FINAL output is unparseable,
 *    record one format miss (bucketed by the verdict's reason), record a
 *    repair attempt, then perform EXACTLY ONE reopen via runClaudeCode with
 *    the caller's focused continuation prompt.
 * 3. Re-extract from the retry output and, on success, record a repair success.
 *
 * Returns the parsed value (or null) plus the miss reason and the retry run
 * result, so callers keep their own terminal-status / failure-category logic.
 * No reopen is attempted when the first attempt already parsed or when
 * exitCode !== 0 (a CLI crash — retrying the same setup wastes a slot).
 */
export async function repairStructuredOutput<T = unknown>(
  opts: RepairStructuredOutputOptions,
): Promise<RepairStructuredOutputResult<T>> {
  const {
    mode, requiredKeys, first, reopenPrompt,
    ws, statusRef, model, effort, disallowedTools, retryStatus, schemaLabel,
    validateParsedValue, run = runClaudeCode,
  } = opts;

  let firstAttempt = tryExtract(
    mode, requiredKeys, buildSources(first.lastResultText, first.allOutput), ws,
  );
  if (firstAttempt.value !== null && validateParsedValue) {
    const validation = validateParsedValue(firstAttempt.value);
    if (!validation.ok) {
      sendLog(ws, `[WARNING] Structured ${mode} parsed output failed schema validation: ${validation.detail}\n`);
      firstAttempt = { value: null, missReason: validation.reason };
    }
  }
  let value = firstAttempt.value as T | null;
  let retry: RunResult | null = null;

  if (value === null && first.exitCode === 0) {
    // Record the unparseable FINAL output from the verdict (ground truth),
    // then that a self-repair re-ask is being attempted. Success is recorded
    // after re-extraction below.
    if (firstAttempt.missReason !== null) recordFormatMiss(firstAttempt.missReason, schemaLabel);
    recordFormatRepairAttempt(schemaLabel);
    sendLog(ws, `[INFO] First structured-output attempt produced no parseable ${mode} (allOutput=${(first.allOutput || '').length}b, lastResultText=${(first.lastResultText || '').length}b). Reopening with focused continuation.\n`);
    if (retryStatus) statusRef.value = retryStatus;

    const priorOutput = first.lastResultText || first.allOutput || '';
    const missAwarePrompt = buildMissAwarePrompt(firstAttempt.missReason, requiredKeys, reopenPrompt, priorOutput);
    const tokensBefore = getLlmTelemetry();
    const retryRun = await run(
      missAwarePrompt, ws, statusRef, model, effort, disallowedTools,
    );
    const tokensAfter = getLlmTelemetry();
    recordFormatRepairTokens(
      tokensAfter.total_input_tokens - tokensBefore.total_input_tokens,
      tokensAfter.total_output_tokens - tokensBefore.total_output_tokens,
      schemaLabel,
    );
    retry = {
      exitCode: retryRun.exitCode,
      allOutput: retryRun.allOutput,
      lastResultText: retryRun.lastResultText,
      codexTurnCompleted: retryRun.codexTurnCompleted,
      stderrTail: retryRun.stderrTail,
    };
    const retryAttempt = tryExtract(
      mode, requiredKeys, buildSources(retryRun.lastResultText, retryRun.allOutput), ws,
    );
    if (retryAttempt.value !== null && validateParsedValue) {
      const validation = validateParsedValue(retryAttempt.value);
      if (!validation.ok) {
        sendLog(ws, `[WARNING] Structured ${mode} retry parsed output failed schema validation: ${validation.detail}\n`);
        retryAttempt.value = null;
        retryAttempt.missReason = validation.reason;
      }
    }
    if (retry.exitCode !== 0) {
      // A non-zero exit (e.g. a filesystem warning) does not invalidate
      // structured output produced during the run — adopt it when parseable.
      recordFormatRepairInfraFailure(schemaLabel);
    }
    value = retryAttempt.value as T | null;
    if (value === null && retry.exitCode === 0 && retryAttempt.missReason !== null) {
      recordFormatRepairMiss(retryAttempt.missReason, schemaLabel);
    }
    // Only count a clean-exit recovery as a success. When the reopen crashed
    // (exitCode !== 0), the attempt is already excluded from the denominator
    // via recordFormatRepairInfraFailure; recording success here too would
    // push self_repair_success_rate above 1.0.
    if (value !== null && retry.exitCode === 0) recordFormatRepairSuccess(schemaLabel);
  }

  return { value, missReason: firstAttempt.missReason, retry };
}
