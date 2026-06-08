import type WebSocket from 'ws';
import { sendLog as _sendLog, runClaudeCode as _runClaudeCode } from '../index.js';
import { DEFAULT_TOOL_GUIDANCE } from '../prompt.js';

type RunClaudeCodeFn = (
  prompt: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string,
  disallowedTools?: string[],
) => Promise<{ exitCode: number; allOutput: string; lastResultText: string; codexTurnCompleted?: boolean; stderrTail?: string }>;

type SendLogFn = (ws: WebSocket | null, message: string) => void;

// Shared bounded self-repair for structured-output handlers.
//
// When a handler exits cleanly (exitCode=0) but the agent's final response
// cannot be parsed into the required structured payload, this helper issues
// ONE focused re-ask that re-states the schema and hands back the prior
// reasoning as bounded context. The caller keeps ownership of re-extraction,
// telemetry recording (recordFormatMiss / recordFormatRepairAttempt /
// recordFormatRepairSuccess), and fall-through to its own terminal behavior.
//
// Design follows the malformed-output auto-repair pattern documented in:
//   - knowledge doc 6f14116c (SWE-agent format-error self-correction)
//   - knowledge doc 5c6a2b4c (Aider malformed-edit auto-repair)
// Key constraints from those studies: repair must be bounded (exactly one
// re-ask, no loops), schema-restating, and return the FULL runner result so
// the caller's failure classifier can inspect codexTurnCompleted / stderrTail.
//
// runFn and logFn are optional injection points for the caller's already-
// resolved imports — callers should pass their locally-imported runClaudeCode
// and sendLog so that test mocks applied to the caller's module scope are
// honoured here too.
export async function attemptStructuredRepair(
  priorText: string,
  schemaHint: string,
  statusLabel: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model: string | undefined,
  effort: string | undefined,
  disallowedTools: string[],
  runFn: RunClaudeCodeFn = _runClaudeCode,
  logFn: SendLogFn = _sendLog,
): Promise<{ exitCode: number; allOutput: string; lastResultText: string; codexTurnCompleted?: boolean; stderrTail?: string }> {
  const context = (priorText || '').slice(-8000);
  logFn(ws, `[INFO] Handler produced no parseable structured output (context=${context.length}b). Reopening with focused continuation.\n`);
  statusRef.value = statusLabel;
  const reopenPrompt = `Your previous response did not end with the required structured JSON output. Please output ONLY the JSON now.

Required format:
${schemaHint}

Your previous analysis (use this to fill the schema):
---
${context}
---

Output ONLY the JSON object now — no preamble, no markdown code fences, no commentary.`;
  return runFn(
    reopenPrompt + `\n\n${DEFAULT_TOOL_GUIDANCE}`,
    ws, statusRef, model, effort, disallowedTools,
  );
}
