import fs from 'fs';
import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  sendHeartbeatSync, setCurrentPromptFingerprint,
} from '../index.js';
import {
  configureAgentPermissions, computePromptFingerprint,
  DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, buildContextPrefix, buildRetryContext, readClaudeMd,
} from '../prompt.js';
import {
  extractJsonObjectDetailed, describeJsonExtractionResult,
} from '../json-extract.js';

// Reads the web-research progress markdown the agent writes for retry context.
// Mirrors readProgressArtifact in research-plan.ts but for the web_research kind
// (that helper hardcodes 'plan' | 'research'). Returns the content prefixed with
// the standard analysis header so buildRetryContext renders it on a retry, or
// undefined when the file is missing/empty.
export function readWebResearchProgress(taskId: string): string | undefined {
  const path = `/tmp/web_research_progress_${taskId}.md`;
  try {
    if (fs.existsSync(path)) {
      const content = fs.readFileSync(path, 'utf-8').trim();
      if (content) return `## Pre-Implementation Analysis\n\n${content}`;
    }
  } catch { /* ignore */ }
  return undefined;
}

// ── Handler: Web Research (web-first investigation, terminal knowledge doc) ───
//
// A web_research task investigates a topic/prompt WEB-FIRST (WebSearch /
// WebFetch) and emits a single structured knowledge DOCUMENT as JSON — title,
// summary, body (markdown), source_citations, tags, topic. It runs strictly
// read-only (no file writes via the Write tool, no PR, no child spawning). The
// backend completion handler (knowledge_completion._handle_web_research_completion)
// parses the document out of the posted `logs`, gates it through the
// forward-quality evaluator, and persists exactly one KnowledgeDocument row.
//
// Modeled on handleMemo (terminal read-only, no clone) with the document
// surfaced both to a /tmp file (read back via readHandlerOutput, robust against
// output truncation) and to the completion logs.
export async function handleWebResearch(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  _ghToken: string, _owner: string, _repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = strVal(payload.prompt) || strVal(payload.topic) || strVal(task.description);
  const topic = strVal(payload.topic);
  if (!prompt) throw new Error('Web research task missing prompt');

  // Web research does not clone the repo — it is web-first. Still install the
  // read_only permission posture (configureAgentPermissions tolerates a missing
  // .git dir) so the Write/Edit/NotebookEdit tools stay disallowed and the
  // permission stance is consistent with other read-only handlers.
  const disallowedTools = configureAgentPermissions('web_research');

  const contextPrefix = buildContextPrefix(task);
  const retryContext = buildRetryContext(task);
  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const topicLine = topic ? `Topic: ${topic}\n\n` : '';

  const documentPath = `/tmp/web_research_document_${taskId}.json`;
  const progressPath = `/tmp/web_research_progress_${taskId}.md`;

  const webResearchPrompt = `${claudeMdPrefix}${retryContext}${contextPrefix}You are a web-research agent. Your job is to investigate the topic below WEB-FIRST — drive WebSearch and WebFetch to gather current, authoritative information — and produce a single structured knowledge DOCUMENT. You operate strictly read-only: you do NOT edit files, you do NOT open a pull request, and you do NOT spawn any follow-up work.

${topicLine}Investigation prompt:
${prompt}

# HOW TO RESEARCH
1. Use WebSearch to find authoritative, current sources on the topic. Use WebFetch to read the most relevant pages in depth.
2. Cross-check claims across multiple sources. Prefer primary/official documentation over secondary commentary.
3. Track the actual URLs you used — every substantive claim in your document must be backed by a citation.
4. Synthesize what you learned into a clear, well-structured markdown document. Be specific and concrete; avoid filler.

# OUTPUT — single knowledge document
Write your document as a JSON object to ${documentPath} (use Bash to write the file — the Write tool is disabled for this read-only task). The file must contain ONLY the JSON object, no markdown wrapping. Schema:

{
  "title": "Concise document title (under ~120 chars)",
  "summary": "1-3 sentence summary of the key findings.",
  "body": "The full document in markdown. Use headings, lists, and short paragraphs. This is the durable knowledge artifact.",
  "source_citations": ["https://example.com/page-you-actually-read", "file:path/to/ref.py (optional code reference)"],
  "tags": ["short-tag-1", "short-tag-2"],
  "topic": "${topic || 'short topic slug'}"
}

Rules:
- title, summary, and body are REQUIRED and must be non-empty strings. body must be substantive markdown grounded in the sources you read.
- source_citations is a list of the web URLs you actually used (plus any file references). Cite the sources behind your claims — an ungrounded document fails the quality gate.
- tags and topic are optional but recommended; tags is a list of short strings.
- Also emit the same JSON object as the VERY LAST thing in your response (in addition to writing the file), so it can be recovered if the file write fails.

ALSO write ${progressPath} — a concise markdown summary (under ~3000 chars) of your investigation: which searches you ran, which sources you read, key findings, and any gaps. This is surfaced to retry agents if this task fails, so they can resume instead of re-investigating.${promptSuffix}

${PLAYBOOK_GUIDANCE}

${DEFAULT_TOOL_GUIDANCE}`;

  statusRef.value = 'Running web research...';
  const fingerprint = computePromptFingerprint('web_research');
  setCurrentPromptFingerprint(fingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: fingerprint });

  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    webResearchPrompt, ws, statusRef, model, effort, disallowedTools,
  );

  // Prefer reading the document from the /tmp file (avoids output truncation);
  // fall back to parsing a JSON object with the required document keys from the
  // final result text or the full transcript.
  let logs = '';
  const documentFromFile = readHandlerOutput(taskId, 'web_research_document', ws);
  if (documentFromFile !== null && typeof documentFromFile === 'object' && !Array.isArray(documentFromFile)) {
    logs = sanitize(JSON.stringify(documentFromFile));
    sendLog(ws, `Read web research document from ${documentPath}\n`);
  }
  if (!logs) {
    const source = (lastResultText && lastResultText.includes('"title"')) ? lastResultText : allOutput;
    const docObj = extractJsonObjectDetailed(source, 'title', 'summary', 'body');
    if (!docObj.ok) {
      sendLog(ws, `[WARNING] Could not extract knowledge document JSON: ${describeJsonExtractionResult(docObj)}\n`);
    }
    logs = sanitize(docObj.ok ? JSON.stringify(docObj.value) : (lastResultText || allOutput.slice(-50_000)));
  }

  const finalStatus = exitCode === 0 ? 'COMPLETED' : 'FAILED';
  const progress = readWebResearchProgress(taskId);
  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: finalStatus,
      ...(finalStatus === 'FAILED'
        ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
        : {}),
      logs,
      ...(progress ? { analysis_plan: progress.slice(0, 5000) } : {}),
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post web research completion status: ${String(e)}\n`);
  }
}
