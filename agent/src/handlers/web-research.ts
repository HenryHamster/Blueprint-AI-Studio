import fs from 'fs';
import { execSync } from 'child_process';
/**
 * Web research handler — search-first knowledge document production.
 *
 * Performs web searches on a research question, fetches and summarizes sources,
 * and produces a structured knowledge document with findings, citations, and
 * confidence levels. Unlike the tactical research handler (which analyzes an
 * existing codebase), web-research begins with external web sources and
 * synthesizes conclusions from them.
 *
 * Payloads specify research_question, number of sources to fetch, and optional
 * scope constraints (time window, domains). The handler uses web search APIs,
 * fetches full pages, extracts key information, and stores the result as a
 * KnowledgeDocument artifact that other tasks can cite via ./fetch knowledge.
 *
 * This task type does not clone a repo or produce a PR; it is artifact-first.
 */

import type WebSocket from 'ws';
import {
  strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  sendHeartbeatSync, setCurrentPromptFingerprint,
} from '../index.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
import {
  configureAgentPermissions, computePromptFingerprint,
  DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, buildContextPrefix, buildRetryContext, readClaudeMd,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, type PlaybookEntry,
} from '../prompt.js';
import { fetchPriorKnowledge, buildPriorKnowledgeSection } from './prior-knowledge.js';
import { repairStructuredOutput } from './structured-output-repair.js';

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

// ── Web-citation reachability grounding ───────────────────────────────────────
//
// Symmetric to design.ts groundDesignCitations, but for the EXTERNAL web URLs a
// web_research document cites: it probes each http(s) citation for reachability
// and stamps the SAME `_citation_grounding` shape the backend/frontend already
// consume ({file_citations, grounded, checked, unresolved}). Here `file_citations`
// means the number of web citations CHECKED. design.ts owns file/path citations;
// this owns web URLs — the two never overlap.
//
// Reachability policy errs HARD toward acceptance so a document is never
// false-failed for an environment problem:
//   - Only http/https citations are probed (file:/path refs are design's domain).
//   - UNRESOLVED only on a clear not-found: DNS resolution failure (ENOTFOUND) or
//     a definitive 404/410.
//   - GROUNDED (benefit of the doubt) for any 2xx/3xx, 403, 429, any 5xx,
//     timeouts, and non-DNS transport errors.
//   - Connectivity guard: a control request to a known-good host runs first; if
//     it fails, outbound connectivity is unavailable and we DEGRADE to
//     `checked: false` (the backend treats an unchecked stamp as a pass).
//   - Bounded: at most the first ~12 citations are checked, each with a short
//     per-request timeout; over-cap citations are simply not counted.

// Cap on the number of web citations probed — keep the work bounded and cheap.
const MAX_WEB_CITATIONS = 12;
// Per-request timeout. Probes run concurrently, so the wall-clock for the whole
// batch is bounded by roughly this value (plus one control request).
const PROBE_TIMEOUT_MS = 3000;
// Known-good control hosts. If NONE return any HTTP status, we have no outbound
// connectivity and must not stamp a checked grounding.
const CONTROL_URLS = ['https://www.google.com', 'https://example.com'];

type FetchImpl = typeof globalThis.fetch;

// Outcome of a single fetch attempt: a real HTTP status, or a transport-layer
// error split into DNS-resolution failure (a clear not-found) vs everything else
// (ambiguous — treated as reachable).
type AttemptResult = { status: number } | { error: 'dns' | 'transport' };

// True when the thrown error indicates DNS resolution failure (NXDOMAIN). Node's
// fetch wraps the underlying error under `.cause`, so check both levels. Only
// ENOTFOUND counts — EAI_AGAIN ("try again") is a transient/no-network signal we
// must NOT treat as a dead host (the connectivity guard handles no-network).
function isDnsFailure(err: unknown): boolean {
  const codes: unknown[] = [];
  if (err && typeof err === 'object') {
    codes.push((err as { code?: unknown }).code);
    const cause = (err as { cause?: unknown }).cause;
    if (cause && typeof cause === 'object') {
      codes.push((cause as { code?: unknown }).code);
    }
  }
  return codes.includes('ENOTFOUND');
}

// One bounded fetch attempt with the given method. Never throws — classifies the
// outcome into a status or a transport/dns error.
async function attemptFetch(
  url: string, method: 'HEAD' | 'GET', fetchImpl: FetchImpl, timeoutMs: number,
): Promise<AttemptResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => { controller.abort(); }, timeoutMs);
  try {
    const res = await fetchImpl(url, { method, redirect: 'follow', signal: controller.signal });
    return { status: res.status };
  } catch (err) {
    return { error: isDnsFailure(err) ? 'dns' : 'transport' };
  } finally {
    clearTimeout(timer);
  }
}

// Probe a single web URL. Returns true when the citation is GROUNDED (reachable
// or ambiguous), false only on a clear not-found (DNS failure or 404/410).
// HEAD-first with a GET fallback when HEAD is unsupported (405/501) or reports a
// not-found (some servers/CDNs misreport HEAD).
async function probeWebCitation(
  url: string, fetchImpl: FetchImpl, timeoutMs: number,
): Promise<boolean> {
  const head = await attemptFetch(url, 'HEAD', fetchImpl, timeoutMs);
  if ('error' in head) return head.error !== 'dns'; // dns = dead; other transport = benefit of doubt
  const notFound = (s: number) => s === 404 || s === 410;
  if (notFound(head.status) || head.status === 405 || head.status === 501) {
    const get = await attemptFetch(url, 'GET', fetchImpl, timeoutMs);
    if ('error' in get) return get.error !== 'dns';
    return !notFound(get.status);
  }
  return true; // any 2xx/3xx/403/429/5xx — grounded by policy
}

// True when outbound connectivity is available: at least one control host
// returns ANY HTTP status (even a 404 proves we reached a server). When every
// control attempt errors at the transport layer, we are offline.
async function hasConnectivity(fetchImpl: FetchImpl, timeoutMs: number): Promise<boolean> {
  for (const url of CONTROL_URLS) {
    const r = await attemptFetch(url, 'HEAD', fetchImpl, timeoutMs);
    if (!('error' in r)) return true;
  }
  return false;
}

// Ground a web_research document's http(s) citations by probing reachability and
// stamp `_citation_grounding` onto the document IN PLACE (mirrors
// groundDesignCitations). Fail-soft: any unexpected error, or an offline VM,
// degrades to `checked: false` so the document is NEVER failed for an
// environment problem. `fetchImpl` is injectable for deterministic tests and
// defaults to the runtime global fetch.
export async function groundWebCitations(
  document: Record<string, unknown>, fetchImpl: FetchImpl = globalThis.fetch,
): Promise<Record<string, unknown>> {
  try {
    const cites = Array.isArray(document.source_citations) ? document.source_citations : [];
    const urls: string[] = [];
    for (const raw of cites) {
      if (typeof raw !== 'string') continue;
      const text = raw.trim();
      if (!/^https?:\/\//i.test(text)) continue; // file:/path refs are design's domain
      urls.push(text);
      if (urls.length >= MAX_WEB_CITATIONS) break; // cap — over-cap not counted
    }
    if (urls.length === 0) {
      document._citation_grounding = {
        file_citations: 0, grounded: 0, checked: true, unresolved: [],
      };
      return document;
    }
    if (!(await hasConnectivity(fetchImpl, PROBE_TIMEOUT_MS))) {
      // No outbound connectivity — degrade to not-checked rather than false-fail.
      document._citation_grounding = {
        file_citations: urls.length, grounded: 0, checked: false, unresolved: [],
      };
      return document;
    }
    // Probe concurrently so total wall-clock is bounded by the per-request
    // timeout rather than the sum across citations.
    const verdicts = await Promise.all(
      urls.map((u) => probeWebCitation(u, fetchImpl, PROBE_TIMEOUT_MS)),
    );
    let grounded = 0;
    const unresolved: string[] = [];
    urls.forEach((u, i) => {
      if (verdicts[i]) grounded += 1;
      else unresolved.push(u); // the offending URL, as written
    });
    document._citation_grounding = {
      file_citations: urls.length, grounded, checked: true, unresolved,
    };
    return document;
  } catch {
    // Unexpected failure — degrade to not-checked; never crash the handler.
    document._citation_grounding = {
      file_citations: 0, grounded: 0, checked: false, unresolved: [],
    };
    return document;
  }
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
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);

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

  const howToResearchGuide = `# HOW TO RESEARCH
1. Use WebSearch to find authoritative, current sources on the topic — run multiple searches in parallel to cover different facets. Use WebFetch to read the most relevant pages in depth.
2. Cross-check claims across multiple sources. Prefer primary/official documentation over secondary commentary.
3. Track the actual URLs you used — every substantive claim in your document must be backed by a citation.
4. Synthesize what you learned into a clear, well-structured markdown document. Be specific and concrete; avoid filler.`;

  const outputSchemaBlock = `# OUTPUT — single knowledge document
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

ALSO write ${progressPath} — a concise markdown summary (under ~3000 chars) of your investigation: which searches you ran, which sources you read, key findings, and any gaps. This is surfaced to retry agents if this task fails, so they can resume instead of re-investigating.`;

  const webResearchPrompt = `${claudeMdPrefix}${webResearchPlaybookSection}${retryContext}${contextPrefix}You are a web-research agent. Your job is to investigate the topic below WEB-FIRST — drive WebSearch and WebFetch to gather current, authoritative information — and produce a single structured knowledge DOCUMENT. You operate strictly read-only: you do NOT edit files, you do NOT open a pull request, and you do NOT spawn any follow-up work.

${topicLine}Investigation prompt:
${prompt}

${priorKnowledgeSection}
${howToResearchGuide}

${outputSchemaBlock}${promptSuffix}

${PLAYBOOK_GUIDANCE}

${DEFAULT_TOOL_GUIDANCE}`;

  const webResearchTelemetry = buildPromptSizeTelemetry(webResearchPrompt, [
    ['claude_md', claudeMdPrefix],
    ['playbook', webResearchPlaybookSection],
    ['retry_context', retryContext],
    ['context_prefix', contextPrefix],
    ['core_body', `You are a web-research agent. Your job is to investigate the topic below WEB-FIRST — drive WebSearch and WebFetch to gather current, authoritative information — and produce a single structured knowledge DOCUMENT. You operate strictly read-only: you do NOT edit files, you do NOT open a pull request, and you do NOT spawn any follow-up work.\n\n${topicLine}Investigation prompt:\n${prompt}\n\n${priorKnowledgeSection}`],
    ['how_to_research', howToResearchGuide],
    ['output_schema', outputSchemaBlock],
    ['prompt_suffix', promptSuffix],
    ['guidance', `\n\n${PLAYBOOK_GUIDANCE}\n\n${DEFAULT_TOOL_GUIDANCE}`],
  ]);
  setPromptSizeTelemetry(webResearchTelemetry.prompt_size_bytes, webResearchTelemetry.prompt_section_count, webResearchTelemetry.prompt_section_sizes);

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
  const progress = readWebResearchProgress(taskId);
  if (!document) {
    const priorAnalysisSection = progress ? `${progress.slice(-8000)}\n\n` : '';
    const repair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['title', 'summary', 'body'],
      schemaLabel: 'web_research',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}You did not emit the knowledge document JSON in the required schema. Output ONLY the JSON object now — no markdown, no explanation. Required fields: title, summary, body. Schema:\n\n{"title":"Concise document title","summary":"1-3 sentence summary.","body":"Full document in markdown.","source_citations":["https://example.com/source"],"tags":["tag"],"topic":"slug"}\n\nOutput ONLY the JSON object.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Retrying knowledge document extraction...',
    });
    if (repair.value !== null) {
      document = repair.value;
    }
  }

  // Embed branch/HEAD provenance INTO the document before serializing — the
  // backend completion handler parses authoring_branch/head_sha out of the
  // parsed `logs` document (knowledge_completion._normalize_document), so
  // provenance must travel inside the document, not as a sibling status field.
  const provenance = captureGitProvenance();
  if (document) {
    // Ground the document's web-URL citations by probing reachability before
    // emitting, so the backend persists which cited URLs are dead. Symmetric to
    // design's file-citation grounding; fail-soft and never blocks completion.
    await groundWebCitations(document);
    logs = sanitize(JSON.stringify({ ...document, ...provenance }));
  } else {
    logs = sanitize(lastResultText || allOutput.slice(-50_000));
  }

  const finalStatus = exitCode === 0 ? 'COMPLETED' : 'FAILED';
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
