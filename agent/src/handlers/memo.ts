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

// Scan raw Codex JSONL output for an item.completed agent_message event and
// return its full item.text. This recovers the memo JSON when allOutput
// contains Codex JSONL envelopes (where the memo JSON is embedded as an
// escaped string in item.text and cannot be found by the normal extractor).
function extractCodexAgentMessageText(text: string): string | null {
  for (const line of text.split('\n')) {
    if (!line.startsWith('{')) continue;
    try {
      const ev = JSON.parse(line) as Record<string, unknown>;
      if (
        (ev.type === 'item.completed' || ev.type === 'item.updated') &&
        ev.item != null && typeof ev.item === 'object'
      ) {
        const item = ev.item as Record<string, unknown>;
        if (item.type === 'agent_message' && typeof item.text === 'string') {
          return item.text;
        }
      }
    } catch { /* not a parseable JSONL envelope — skip */ }
  }
  return null;
}

// ── Handler: Memo (terminal reasoning artifact, no repo clone, no PR) ────────
//
// Memo tasks document a piece of reasoning that strategic review can cite
// later. The agent receives whatever context is on the payload (topic,
// reasoning_kind, source_finding, etc.) and emits a single JSON object
// describing the reasoning. The backend persists that object as an Artifact
// of type MEMO. No cloning, no edits, no child spawning.

// For dispatcher-sourced memos the backend already holds the complete
// reasoning in source_finding at spawn time. Synthesize the artifact
// directly instead of invoking Claude to paraphrase already-known text.
function _synthesizeDispatcherMemo(
  reasoningKind: string,
  sf: Record<string, unknown>,
  sourceTaskId: string,
  sv: (v: unknown) => string,
): Record<string, unknown> | null {
  const cite = sourceTaskId ? [`task:${sourceTaskId}`] : [];
  if (reasoningKind === 'regression_false_positive') {
    const verdictReason = sv(sf.verdict_reason);
    if (!verdictReason) return null;
    const parts = [`Verdict reason: ${verdictReason}`];
    const invSummary = sv(sf.investigation_summary);
    if (invSummary) parts.push(`Investigation summary: ${invSummary}`);
    const rc = sv(sf.regression_class);
    return { summary: verdictReason.slice(0, 200), reasoning: parts.join('\n\n'), citations: cite, tags: ['regression-false-positive', ...(rc ? [rc] : [])] };
  }
  if (reasoningKind === 'investigation_unactionable') {
    const rootCause = sv(sf.root_cause);
    const summary = sv(sf.summary);
    if (!rootCause && !summary) return null;
    const parts: string[] = [];
    if (rootCause) parts.push(`Root cause: ${rootCause}`);
    const fix = sv(sf.recommended_fix);
    if (fix) parts.push(`Recommended fix: ${fix}`);
    const outcome = sv(sf.outcome);
    if (outcome) parts.push(`Outcome: ${outcome}`);
    return { summary: summary.slice(0, 200) || `Investigation concluded: ${outcome}`, reasoning: parts.join('\n\n'), citations: cite, tags: ['investigation-unactionable', ...(outcome ? [outcome] : [])] };
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
    const directMemo = _synthesizeDispatcherMemo(reasoningKind, sourceFinding as Record<string, unknown>, sourceTaskId, strVal);
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
  "citations": ["task:<id>", "direction:<id>", "file:path/to/x.py", ...],
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

  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    memoPrompt + `\n\n${DEFAULT_TOOL_GUIDANCE}`,
    ws, statusRef, model, effort, disallowedTools,
  );

  // Prefer the final result text (cheapest, usually clean JSON) and fall
  // back to scanning the full transcript for the schema's required keys.
  let memoObj: Record<string, unknown> | null = null;
  const requiredKeys = ['summary', 'reasoning', 'citations', 'tags'];
  const sources: Array<{ label: string; text: string }> = [];
  if (lastResultText) sources.push({ label: 'lastResultText', text: lastResultText });
  if (allOutput) sources.push({ label: 'allOutput', text: allOutput });
  // If allOutput contains raw Codex JSONL, the memo JSON is embedded as an
  // escaped string in item.text and the normal extractor cannot reach it.
  // Parse the envelopes to recover the unescaped inner text as a third source.
  if (allOutput) {
    const codexText = extractCodexAgentMessageText(allOutput);
    if (codexText) sources.push({ label: 'allOutput[codex-agent_message]', text: codexText });
  }

  for (const src of sources) {
    if (!src.text.includes('"summary"') || !src.text.includes('"reasoning"')) continue;
    try {
      const res = extractJsonObjectDetailed(src.text, 'summary', 'reasoning');
      if (!res.ok) {
        sendLog(ws, `[WARNING] Could not parse memo JSON from ${src.label}: ${describeJsonExtractionResult(res)}\n`);
        continue;
      }
      const obj = res.value;
      const missing = requiredKeys.filter(k => !(k in obj));
      if (missing.length > 0) {
        sendLog(ws, `[WARNING] Memo JSON from ${src.label} missing required keys: ${missing.join(', ')}\n`);
        continue;
      }
      memoObj = obj;
      break;
    } catch (e) {
      sendLog(ws, `[WARNING] Memo JSON extraction from ${src.label} failed: ${e instanceof JsonExtractionError ? e.message : String(e)}\n`);
    }
  }

  if (memoObj === null) {
    const tail = (allOutput || '').slice(-2000);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'parse_failed',
      logs:
        `Memo handler could not extract a JSON object with all required keys ` +
        `(summary, reasoning, citations, tags). exitCode=${exitCode}, ` +
        `allOutput=${(allOutput || '').length}b, ` +
        `lastResultText=${(lastResultText || '').length}b\n` +
        `--- allOutput tail ---\n${tail}`,
    });
    return;
  }

  const logs = sanitize(JSON.stringify(memoObj));
  // On success we deliberately OMIT artifact_type. The backend status
  // endpoint creates a generic Artifact row whenever artifact_type is set
  // (agent_status.py: `if payload.artifact_type:`). The memo completion
  // handler (_handle_memo_completion) is the SOLE writer of the MEMO
  // Artifact — posting artifact_type='MEMO' here would create a second,
  // duplicate row and make citation-by-id ambiguous. Mirrors handlePlan,
  // which also omits artifact_type on its success path.
  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
    ...(exitCode !== 0
      ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
      : {}),
    logs,
  });
}
