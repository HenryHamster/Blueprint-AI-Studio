/**
 * Memo task handler — terminal reasoning artifact (no repo, no PR).
 *
 * Memo tasks emit a structured reasoning artifact (summary, reasoning, citations,
 * tags) without cloning a repo, making edits, or creating a PR. Used by the
 * orchestrator to document strategic decisions (e.g., direction retirement),
 * intermediate analysis, or findings that future tasks can cite.
 *
 * The agent receives context (reasoning_kind, source_finding, direction, etc.)
 * and outputs a JSON object with required keys: summary, reasoning, citations,
 * and tags. If the JSON is malformed, a bounded one-shot continuation repair
 * re-opens the task to fix the format.
 *
 * The backend records the memo into the task's plan_output['memo'] (its sole,
 * canonical store); no separate MEMO Artifact row is written.
 */

import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, persistAndPostStatus, runClaudeCode,
  sendHeartbeatSync, setCurrentPromptFingerprint,
} from '../index.js';
import {
  configureAgentPermissions, computePromptFingerprint,
  DEFAULT_TOOL_GUIDANCE, buildContextPrefix,
} from '../prompt.js';
import { extractJsonObjectDetailed, describeJsonExtractionResult, JsonExtractionError } from '../json-extract.js';
import {
  recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess,
  recordFormatRepairMiss,
  type FormatMissReason,
  buildPromptSizeTelemetry, setPromptSizeTelemetry,
} from '../telemetry.js';
import { attemptStructuredRepair } from './structured-repair.js';

// Scan raw Codex JSONL output for agent_message events and return ALL
// candidate item.text values, prioritising item.completed over item.updated
// and longer texts over shorter within each group. This recovers the memo
// JSON when allOutput contains Codex JSONL envelopes (where the memo JSON is
// embedded as an escaped string in item.text and cannot be found by the
// normal extractor). Returning all candidates lets tryExtractMemoObject pick
// the first one that yields a valid memo JSON with all required keys.
function extractCodexAgentMessageTexts(text: string): string[] {
  const completedTexts: string[] = [];
  const updatedTexts: string[] = [];
  for (const line of text.split('\n')) {
    if (!line.startsWith('{')) continue;
    try {
      const ev = JSON.parse(line) as Record<string, unknown>;
      if (ev.item != null && typeof ev.item === 'object') {
        const item = ev.item as Record<string, unknown>;
        if (item.type === 'agent_message' && typeof item.text === 'string') {
          if (ev.type === 'item.completed') {
            completedTexts.push(item.text);
          } else if (ev.type === 'item.updated') {
            updatedTexts.push(item.text);
          }
        }
      }
    } catch { /* not a parseable JSONL envelope — skip */ }
  }
  // Within each group sort by descending length so the most complete text comes first.
  completedTexts.sort((a, b) => b.length - a.length);
  updatedTexts.sort((a, b) => b.length - a.length);
  // item.completed takes priority over item.updated.
  return [...completedTexts, ...updatedTexts];
}

const MEMO_SCHEMA_HINT =
  `{\n  "summary": "One- to two-sentence summary of the reasoning.",\n  "reasoning": "Detailed reasoning (a few paragraphs at most).",\n  "citations": ["task:<id>", "direction:<id>", "file:path/to/source", ...],\n  "tags": ["tag-1", "tag-2", ...]\n}\nAll four keys are required. summary and reasoning must be non-empty strings; citations and tags must be arrays of strings (empty arrays allowed).`;

// ── Handler: Memo (terminal reasoning artifact, no repo clone, no PR) ────────
//
// Memo tasks document a piece of reasoning that strategic review can cite
// later. The agent receives whatever context is on the payload (topic,
// reasoning_kind, source_finding, etc.) and emits a single JSON object
// describing the reasoning. The backend records that object into the task's
// plan_output['memo']. No cloning, no edits, no child spawning.

// Reasoning kinds the dispatcher synthesizer handles credential-free (i.e.
// _synthesizeDispatcherMemo returns non-null for these values). Exported so
// unit tests can assert every bake-smoke fixture uses a kind in this set —
// catching a mismatch at test time instead of at Packer image build time.
export const DISPATCHER_MEMO_SYNTHESIZER_KINDS = ['direction_retirement'] as const satisfies readonly string[];

// For dispatcher-sourced memos the backend already holds the complete
// reasoning in source_finding at spawn time. Synthesize the artifact
// directly instead of invoking Claude to paraphrase already-known text.
function _synthesizeDispatcherMemo(
  reasoningKind: string,
  sf: Record<string, unknown>,
  sourceTaskId: string,
  directionId: string,
  sv: (v: unknown) => string,
): Record<string, unknown> | null {
  const cite = [
    directionId ? `direction:${directionId}` : null,
    sourceTaskId ? `task:${sourceTaskId}` : null,
  ].filter((x): x is string => x !== null);
  if (reasoningKind === 'direction_retirement') {
    const directionText = sv(sf.direction_text);
    const rationale = sv(sf.rationale);
    if (!directionText && !rationale) return null;
    const parts: string[] = [];
    if (directionText) parts.push(`Direction: ${directionText}`);
    const rationaleOrDefault = rationale || 'Direction retired by automated review.';
    parts.push(`Retirement rationale: ${rationaleOrDefault}`);
    const se = sv(sf.success_evidence);
    if (se) parts.push(`Success evidence: ${se}`);
    const rc = sv(sf.retirement_condition);
    if (rc) parts.push(`Retirement condition: ${rc}`);
    const rm = sf.retirement_metrics != null && typeof sf.retirement_metrics === 'object'
      ? sf.retirement_metrics as Record<string, unknown>
      : {};
    const qs = typeof rm.final_quality_score === 'number' ? String(rm.final_quality_score) : null;
    const pm = typeof rm.final_prs_merged === 'number' ? String(rm.final_prs_merged) : null;
    const ca = typeof rm.final_cycles_active === 'number' ? String(rm.final_cycles_active) : null;
    if (qs || pm || ca) {
      const metrics = [qs && `quality_score=${qs}`, pm && `prs_merged=${pm}`, ca && `cycles_active=${ca}`].filter(Boolean).join(', ');
      parts.push(`Outcome metrics: ${metrics}`);
    }
    const rhs = sf.recent_history_summary;
    if (Array.isArray(rhs) && rhs.length > 0) {
      const histLines = (rhs as unknown[]).map((h) => {
        if (h == null || typeof h !== 'object') return null;
        const hr = h as Record<string, unknown>;
        const hParts = [];
        if (typeof hr.cycles === 'number') hParts.push(`cycles=${hr.cycles}`);
        if (typeof hr.prs_merged === 'number') hParts.push(`prs_merged=${hr.prs_merged}`);
        if (typeof hr.tasks_completed === 'number') hParts.push(`tasks_completed=${hr.tasks_completed}`);
        if (typeof hr.tasks_failed === 'number') hParts.push(`tasks_failed=${hr.tasks_failed}`);
        return hParts.length > 0 ? hParts.join(', ') : null;
      }).filter((x): x is string => x !== null);
      if (histLines.length > 0) {
        parts.push(`Recent history: ${histLines.join(' | ')}`);
      }
    }
    const summary = (rationale || directionText || 'Direction retired.').slice(0, 200);
    return { summary, reasoning: parts.join('\n\n'), citations: cite, tags: ['direction-retirement'] };
  }
  return null;
}

export async function handleMemo(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  _ghToken: string, _owner: string, _repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const topic = strVal(payload.topic);
  const reasoningKind = strVal(payload.reasoning_kind);
  const sourceTaskId = strVal(payload.source_task_id);
  const directionId = strVal(payload.direction_id);
  const userPrompt = strVal(payload.prompt) || strVal(task.description);
  const sourceFinding = payload.source_finding;

  // Short-circuit for dispatcher memos: synthesize from source_finding directly.
  if (sourceFinding && typeof sourceFinding === 'object') {
    const directMemo = _synthesizeDispatcherMemo(reasoningKind, sourceFinding as Record<string, unknown>, sourceTaskId, directionId, strVal);
    if (directMemo !== null) {
      await persistAndPostStatus(taskId, { task_id: taskId, status: 'COMPLETED', logs: sanitize(JSON.stringify(directMemo)) });
      return;
    }
  }

  // Memo handlers must not touch the repo — configureAgentPermissions
  // installs the read_only hooks even without a clone (it tolerates a
  // missing .git dir) so the agent's permission posture stays consistent.
  const disallowedTools = configureAgentPermissions('memo');

  const contextPrefix = buildContextPrefix(task);

  const topicSection = topic ? `Topic: ${topic}\n` : '';
  const kindSection = reasoningKind ? `Reasoning kind: ${reasoningKind}\n` : '';
  const directionSection = directionId ? `Direction id: ${directionId}\n` : '';
  const sourceTaskSection = sourceTaskId ? `Source task id: ${sourceTaskId}\n` : '';
  const sourceFindingSection = sourceFinding
    ? `Source finding (JSON):\n${JSON.stringify(sourceFinding, null, 2)}\n\n`
    : '';
  const promptSection = userPrompt ? `\nReasoning prompt:\n${userPrompt}\n` : '';

  const memoPrompt = `${contextPrefix}You are a memo agent. Your job is to produce a single structured piece of reasoning that will be persisted as a citable artifact. You do NOT have access to a repository clone, you do NOT edit code, and you do NOT spawn any follow-up work.

${topicSection}${kindSection}${directionSection}${sourceTaskSection}${sourceFindingSection}${promptSection}
Instructions:
1. Think through the topic / source material above and produce a concise piece of reasoning.
2. Cite any context that informed the reasoning (other task ids, direction ids, file paths from the source_finding, prior memos). Citations are free-form strings — keep them short and recognizable.
3. Add a few short tags (single words or short phrases) so strategic review can group related memos.
4. Output your memo as a single JSON object — and ONLY that object — as the VERY LAST thing in your response. Schema:

{
  "summary": "One- to two-sentence summary of the reasoning.",
  "reasoning": "Detailed reasoning (a few paragraphs at most).",
  "citations": ["task:<id>", "direction:<id>", "file:path/to/source", ...],
  "tags": ["tag-1", "tag-2", ...]
}

Rules:
- All four keys are required.
- summary and reasoning must be non-empty strings.
- citations and tags must be arrays of strings (empty arrays are allowed).
- Do NOT wrap the JSON in markdown code fences in your final message — emit the bare object.
- Do NOT modify any files. This task is read-only by design.`;

  statusRef.value = 'Writing memo...';
  const fingerprint = computePromptFingerprint('memo');
  setCurrentPromptFingerprint(fingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: fingerprint });

  const requiredKeys = ['summary', 'reasoning', 'citations', 'tags'];

  // Try to extract a valid memo object from any of the given text sources.
  // Returns the parsed object on first match (with missReason null), or null
  // with the extraction layer's reason for the FIRST schema-bearing source that
  // failed — so the caller can record format-compliance telemetry straight from
  // the verdict (it is GROUND TRUTH; do not re-derive). `malformed_json` is the
  // reason for a thrown JsonExtractionError (candidate parsed-but-malformed).
  const tryExtractMemoObject = (
    sources: Array<{ label: string; text: string }>,
  ): { value: Record<string, unknown> | null; missReason: FormatMissReason | null } => {
    let missReason: FormatMissReason | null = null;
    for (const src of sources) {
      if (!src.text.includes('"summary"') || !src.text.includes('"reasoning"')) continue;
      try {
        const res = extractJsonObjectDetailed(src.text, 'summary', 'reasoning');
        if (!res.ok) {
          if (missReason === null) missReason = res.reason;
          sendLog(ws, `[WARNING] Could not parse memo JSON from ${src.label}: ${describeJsonExtractionResult(res)}\n`);
          continue;
        }
        const obj = res.value;
        // Defense-in-depth: extractJsonObjectDetailed now validates own-property
        // membership as the primary gate, so this path is rarely reached.
        const missing = requiredKeys.filter(k => !(k in obj));
        if (missing.length > 0) {
          if (missReason === null) missReason = 'parsed_object_missing_key';
          sendLog(ws, `[WARNING] Memo JSON from ${src.label} missing required keys: ${missing.join(', ')}\n`);
          continue;
        }
        return { value: obj, missReason: null };
      } catch (e) {
        if (missReason === null) missReason = 'malformed_json';
        sendLog(ws, `[WARNING] Memo JSON extraction from ${src.label} failed: ${e instanceof JsonExtractionError ? e.message : String(e)}\n`);
      }
    }
    return { value: null, missReason };
  };

  const buildSources = (
    lastResultText: string,
    allOutput: string,
  ): Array<{ label: string; text: string }> => {
    const sources: Array<{ label: string; text: string }> = [];
    if (lastResultText) sources.push({ label: 'lastResultText', text: lastResultText });
    if (allOutput) sources.push({ label: 'allOutput', text: allOutput });
    if (allOutput) {
      const codexTexts = extractCodexAgentMessageTexts(allOutput);
      codexTexts.forEach((t, idx) => {
        sources.push({ label: `allOutput[codex-agent_message#${idx}]`, text: t });
      });
    }
    return sources;
  };

  const memoFullPrompt = memoPrompt + `\n\n${DEFAULT_TOOL_GUIDANCE}`;
  const memoTelemetry = buildPromptSizeTelemetry(memoFullPrompt, [
    ['context_prefix', contextPrefix],
    ['topic', topicSection],
    ['reasoning_kind', kindSection],
    ['direction', directionSection],
    ['source_task', sourceTaskSection],
    ['source_finding', sourceFindingSection],
    ['prompt', promptSection],
    ['tool_guidance', DEFAULT_TOOL_GUIDANCE],
  ]);
  setPromptSizeTelemetry(memoTelemetry.prompt_size_bytes, memoTelemetry.prompt_section_count, memoTelemetry.prompt_section_sizes);

  const {
    exitCode, allOutput, lastResultText,
    codexTurnCompleted, stderrTail,
  } = await runClaudeCode(
    memoFullPrompt,
    ws, statusRef, model, effort, disallowedTools,
  );

  // Prefer the final result text (cheapest, usually clean JSON) and fall
  // back to scanning the full transcript for the schema's required keys.
  const firstAttempt = tryExtractMemoObject(buildSources(lastResultText, allOutput));
  let memoObj = firstAttempt.value;
  let retryExitCode: number | null = null;
  let retryAllOutput = '';
  let retryLastResultText = '';
  // `undefined` means "no retry ran"; a real Codex run may return either
  // boolean (turn observed or not). Non-Codex runners return undefined too,
  // which collapses to "no cause-C evidence" in the classifier below.
  let retryCodexTurnCompleted: boolean | undefined;
  let retryStderrTail = '';

  // Reopen path: the agent often produces a clean exit (exitCode=0, ~2KB of
  // narrative) but drifts off-spec and never emits the JSON object. Instead
  // of FAILing on the first such drift, reopen with a focused continuation
  // prompt that re-states the schema and asks for ONLY the JSON. The retry
  // is itself a fresh Claude session (stateless), so we hand it the prior
  // reasoning as context to fill the schema fields from.
  if (memoObj === null && exitCode === 0) {
    // Record the initial unparseable FINAL output straight from the extraction
    // verdict (ground truth), then that a self-repair re-ask is being attempted.
    // Whether it succeeded is recorded after re-extraction below.
    if (firstAttempt.missReason !== null) recordFormatMiss(firstAttempt.missReason, 'memo');
    recordFormatRepairAttempt('memo');
    // attemptStructuredRepair is the single recorder of repair-call token deltas
    // (recordFormatRepairTokens) — passing schemaLabel keeps the per-schema
    // attribution. Do NOT re-snapshot here or tokens double-count.
    const retryRun = await attemptStructuredRepair(
      lastResultText || allOutput, MEMO_SCHEMA_HINT,
      'Writing memo (retry)...',
      ws, statusRef, model, effort, disallowedTools,
      runClaudeCode, sendLog, 'memo',
    );
    retryExitCode = retryRun.exitCode;
    retryAllOutput = retryRun.allOutput;
    retryLastResultText = retryRun.lastResultText;
    retryCodexTurnCompleted = retryRun.codexTurnCompleted;
    retryStderrTail = retryRun.stderrTail || '';
    const retryAttempt = tryExtractMemoObject(
      buildSources(retryRun.lastResultText, retryRun.allOutput),
    );
    memoObj = retryAttempt.value;
    if (memoObj !== null) {
      recordFormatRepairSuccess('memo');
    } else if (retryExitCode === 0 && retryAttempt.missReason !== null) {
      recordFormatRepairMiss(retryAttempt.missReason, 'memo');
    }
  }

  if (memoObj === null) {
    const tail = (retryAllOutput || allOutput || '').slice(-2000);
    const retrySuffix = retryExitCode !== null
      ? `, retryExitCode=${retryExitCode}, retryAllOutput=${(retryAllOutput || '').length}b`
      : '';

    // ── Failure-category selection ──────────────────────────────────────────
    // Three distinct memo-extraction failures exist as of 2026-05-26:
    //   (1) exitCode !== 0           — Claude/Codex CLI crash. The downstream
    //       backend already has rate-limit/account-rotation overrides for
    //       `claude_code_failed` (see _ALL_GENERIC_CODE_ERROR_LABELS in
    //       backend/app/failure_classification.py).
    //   (2) exitCode == 0 + Codex stream missing `turn.completed` — provider
    //       silently disconnected mid-turn (token cap, content refusal,
    //       upstream 5xx that Codex swallows as success). The agent never
    //       had a chance to emit JSON — labeling this `parse_failed` was
    //       misleading and bypassed rate-limit overrides. Reclassify to
    //       `claude_code_failed` so account-state correlation kicks in.
    //   (3) exitCode == 0 + agent emitted output WITHOUT the schema markers
    //       (`"summary"` / `"reasoning"`) — wrapper-fallback territory; the
    //       agent never tried to produce the JSON. Emit `incomplete` so
    //       analytics can separate it from genuine parse failures.
    //   (4) otherwise — agent did emit schema-shaped text but it didn't
    //       parse / was missing keys — keep the historical `parse_failed`.
    //
    // Use the most recent run's signals when a retry ran; fall back to the
    // first run otherwise. retryExitCode === null means no retry happened
    // (e.g. exitCode !== 0 path skipped the reopen).
    const retryRan = retryExitCode !== null;
    const finalExitCode = retryRan ? (retryExitCode as number) : exitCode;
    // codexTurnCompleted is undefined for non-Codex runners and for test
    // stubs. We treat undefined as "no evidence either way" — only an
    // explicit `false` plus a thread.started marker triggers cause-C.
    const finalCodexTurnCompleted = retryRan ? retryCodexTurnCompleted : codexTurnCompleted;
    // Cause-C detection: only the latest run's stream tells us whether THIS
    // run reached turn.completed. Use retry's allOutput when a retry ran.
    const causeCAllOutput = retryRan ? retryAllOutput : allOutput;
    // Schema-attempt detection (Layer B): union of BOTH runs' outputs, so a
    // schema-shaped attempt in the first run that produced incomplete JSON
    // doesn't get downgraded to `incomplete` just because the retry was
    // silent. The `parse_failed` label is informative when EITHER run tried
    // to emit the schema.
    const schemaAttemptText =
      `${lastResultText || ''}\n${allOutput || ''}\n`
      + `${retryLastResultText || ''}\n${retryAllOutput || ''}`;
    const finalStderrTail = retryStderrTail || stderrTail || '';

    let failureCategory: 'parse_failed' | 'claude_code_failed' | 'incomplete';
    if (finalExitCode !== 0) {
      failureCategory = 'claude_code_failed';
    } else if (
      finalCodexTurnCompleted === false
      && causeCAllOutput.includes('"type":"thread.started"')
    ) {
      // Cause-C: Codex emitted thread.started but never turn.completed
      // despite exitCode=0. Treat as a CLI-side failure so the backend's
      // rate-limit / account-rotation overrides apply. Explicit `=== false`
      // gate: undefined (non-Codex / test stub) is NOT treated as evidence.
      failureCategory = 'claude_code_failed';
    } else {
      // Layer B: parse_failed should only fire when the agent actually
      // tried to emit the schema. Otherwise the agent silently bailed —
      // `incomplete` is the canonical wrapper-fallback label.
      const looksLikeMemoAttempt =
        schemaAttemptText.includes('"summary"') || schemaAttemptText.includes('"reasoning"');
      failureCategory = looksLikeMemoAttempt ? 'parse_failed' : 'incomplete';
    }

    const stderrSuffix = finalStderrTail
      ? `\n--- stderr tail (${finalStderrTail.length}b) ---\n${finalStderrTail}`
      : '';
    const codexSuffix = causeCAllOutput.includes('"type":"thread.started"')
      ? `, codexTurnCompleted=${String(finalCodexTurnCompleted)}`
      : '';

    // If any agent_message candidates were found in the most recent run's output,
    // append each one verbatim (capped at 64 KB) so the backend recovery path
    // can re-attempt extraction even when the output tail was truncated.
    const recoveredCandidates = extractCodexAgentMessageTexts(causeCAllOutput);
    const recoveredSuffix = recoveredCandidates.length > 0
      ? recoveredCandidates.map((candidate) =>
          `\n--- recovered-agent-message-text ---\n${candidate.slice(0, 65536)}`
        ).join('')
      : '';

    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: failureCategory,
      logs: sanitize(
        `Memo handler could not extract a JSON object with all required keys ` +
        `(summary, reasoning, citations, tags). exitCode=${exitCode}, ` +
        `allOutput=${(allOutput || '').length}b, ` +
        `lastResultText=${(lastResultText || '').length}b${retrySuffix}${codexSuffix}\n` +
        `--- output tail ---\n${tail}${stderrSuffix}${recoveredSuffix}`
      ),
    });
    return;
  }

  const logs = sanitize(JSON.stringify(memoObj));
  // On success we deliberately OMIT artifact_type. The backend status
  // endpoint creates a generic Artifact row whenever artifact_type is set
  // (agent_status.py: `if payload.artifact_type:`). The memo completion
  // handler (_handle_memo_completion) records the memo into plan_output and
  // writes NO Artifact row — posting artifact_type='MEMO' here would create a
  // stray Artifact row nothing reads. Mirrors handlePlan, which also omits
  // artifact_type on its success path.
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0
      ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
      : {}),
    logs,
  });
}
