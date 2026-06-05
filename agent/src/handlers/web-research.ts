import fs from 'fs';
import { execSync } from 'child_process';
import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  sendHeartbeatSync, setCurrentPromptFingerprint, apiGet,
} from '../index.js';
import {
  configureAgentPermissions, computePromptFingerprint,
  DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, buildContextPrefix, buildRetryContext, readClaudeMd,
  formatPlaybook, type PlaybookEntry,
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

// Capture the git branch and short HEAD commit from the current working directory.
// Web research does not clone a repo, so this returns an empty object. Tolerates
// non-git or missing workspace by returning an empty object on error.
function captureGitProvenance(): Record<string, string> {
  try {
    const branch = execSync('git rev-parse --abbrev-ref HEAD').toString().trim();
    const sha = execSync('git rev-parse --short HEAD').toString().trim();
    const result: Record<string, string> = {};
    if (branch) result.authoring_branch = branch;
    if (sha) result.head_sha = sha;
    return result;
  } catch {
    return {};
  }
}

// A single knowledge-document summary as returned by the agent-facing list
// endpoint (GET /api/agent/knowledge-documents). The list shape omits the
// markdown body; only id/title/summary/topic/tags are present.
interface PriorKnowledgeDoc {
  id: string;
  title: string;
  summary: string;
}

// Cap on injected prior-knowledge docs — keep the prompt focused on the most
// relevant handful rather than dumping the whole store.
const PRIOR_KNOWLEDGE_LIMIT = 5;

// Fetch the most relevant existing knowledge documents for this task using the
// task's topic/prompt as the full-text search query, via the agent's
// authenticated backend client (apiGet attaches the X-Agent-Token). Uses the
// existing trigram/ILIKE full-text search (the `q` term) — no embeddings.
//
// Fail-soft by contract: any error, timeout, or empty result returns [] so the
// caller proceeds exactly as it would with no prior knowledge. A knowledge
// fetch must NEVER break a research run.
async function fetchPriorKnowledge(queryText: string): Promise<PriorKnowledgeDoc[]> {
  const q = queryText.trim();
  if (!q) return [];
  try {
    const path = `/api/agent/knowledge-documents?q=${encodeURIComponent(q)}&limit=${PRIOR_KNOWLEDGE_LIMIT}`;
    const res = await apiGet(path) as { documents?: unknown };
    const docs = Array.isArray(res.documents) ? res.documents : [];
    return docs
      .filter((d): d is Record<string, unknown> => !!d && typeof d === 'object')
      .map((d) => ({
        id: strVal(d.id),
        title: strVal(d.title),
        summary: strVal(d.summary),
      }))
      .filter((d) => d.id && d.title)
      .slice(0, PRIOR_KNOWLEDGE_LIMIT);
  } catch {
    return [];
  }
}

// Build a clearly delimited `## Prior Knowledge` section listing each retrieved
// document's title, summary, and id, with an explicit instruction to build on /
// extend / supersede the prior knowledge and cite the relevant document ids
// rather than re-deriving from scratch. Returns '' when there are no documents
// so the prompt is byte-for-byte unchanged in the no-knowledge case.
function buildPriorKnowledgeSection(docs: PriorKnowledgeDoc[]): string {
  if (docs.length === 0) return '';
  const entries = docs
    .map((d) => `- [${d.id}] ${d.title}\n  ${d.summary || '(no summary)'}`)
    .join('\n');
  return `## Prior Knowledge
The knowledge store already holds the following related document(s) for this project. Build ON this prior knowledge: extend, refine, or supersede it rather than re-deriving from scratch, and cite the relevant document id(s) (e.g. "supersedes [<id>]") where your findings update or contradict them.

${entries}

`;
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
  const webResearchPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const topicLine = topic ? `Topic: ${topic}\n\n` : '';

  const documentPath = `/tmp/web_research_document_${taskId}.json`;
  const progressPath = `/tmp/web_research_progress_${taskId}.md`;

  // Pull the most relevant existing knowledge documents (topic preferred, else
  // the prompt) so the run builds on prior knowledge instead of starting from a
  // blank slate. Fail-soft: an empty list yields no section and no failure.
  const priorDocs = await fetchPriorKnowledge(topic || prompt);
  if (priorDocs.length > 0) {
    sendLog(ws, `Injecting ${priorDocs.length} prior knowledge document(s) into the research prompt\n`);
  }
  const priorKnowledgeSection = buildPriorKnowledgeSection(priorDocs);

  const webResearchPrompt = `${claudeMdPrefix}${webResearchPlaybookSection}${retryContext}${contextPrefix}You are a web-research agent. Your job is to investigate the topic below WEB-FIRST — drive WebSearch and WebFetch to gather current, authoritative information — and produce a single structured knowledge DOCUMENT. You operate strictly read-only: you do NOT edit files, you do NOT open a pull request, and you do NOT spawn any follow-up work.

${topicLine}Investigation prompt:
${prompt}

${priorKnowledgeSection}
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
- source_citations is REQUIRED and must contain at least one usable citation — a web URL (https://...) or a file reference (file:... or a path). Cite the sources behind your claims: an ungrounded document with no usable citation FAILS the forward-quality gate and is discarded.
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
  let document: Record<string, unknown> | null = null;
  const documentFromFile = readHandlerOutput(taskId, 'web_research_document', ws);
  if (documentFromFile !== null && typeof documentFromFile === 'object' && !Array.isArray(documentFromFile)) {
    document = documentFromFile as Record<string, unknown>;
    sendLog(ws, `Read web research document from ${documentPath}\n`);
  }
  if (!document) {
    const source = (lastResultText && lastResultText.includes('"title"')) ? lastResultText : allOutput;
    const docObj = extractJsonObjectDetailed(source, 'title', 'summary', 'body');
    if (!docObj.ok) {
      sendLog(ws, `[WARNING] Could not extract knowledge document JSON: ${describeJsonExtractionResult(docObj)}\n`);
    } else {
      document = docObj.value;
    }
  }

  // Embed branch/HEAD provenance INTO the document before serializing — the
  // backend completion handler parses authoring_branch/head_sha out of the
  // parsed `logs` document (knowledge_completion._normalize_document), so
  // provenance must travel inside the document, not as a sibling status field.
  const provenance = captureGitProvenance();
  if (document) {
    logs = sanitize(JSON.stringify({ ...document, ...provenance }));
  } else {
    logs = sanitize(lastResultText || allOutput.slice(-50_000));
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
