import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode, validateBranchName, resolveBaseBranch,
  sendHeartbeatSync, setCurrentPromptFingerprint,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { DEFAULT_CLONE_DEPTH } from '../constants.js';
import {
  configureAgentPermissions, computePromptFingerprint, buildFileTree,
  DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, buildContextPrefix, buildRetryContext, readClaudeMd,
  writePlaybookHelper, writeMessagesHelper, writeDesignDocHelper, formatPlaybook, type PlaybookEntry,
} from '../prompt.js';
import {
  writeFetchHelper, buildFetchGuidance, PLAN_FETCH_VERBS,
} from './strategic-helpers.js';
import { fetchPriorKnowledge, buildPriorKnowledgeSection } from './prior-knowledge.js';
import { repairStructuredOutput } from './structured-output-repair.js';
import { extractJsonObject } from '../json-extract.js';
import { buildAvailableCapabilitiesSection, CAPABILITY_DECLARATION_DOC } from './research-plan.js';

// Reads the design progress markdown the agent writes for retry context.
// Mirrors readWebResearchProgress but for the design kind. Returns the content
// prefixed with the standard analysis header so buildRetryContext renders it on
// a retry, or undefined when the file is missing/empty.
export function readDesignProgress(taskId: string): string | undefined {
  const path = `/tmp/design_progress_${taskId}.md`;
  try {
    if (fs.existsSync(path)) {
      const content = fs.readFileSync(path, 'utf-8').trim();
      if (content) return `## Pre-Implementation Analysis\n\n${content}`;
    }
  } catch { /* ignore */ }
  return undefined;
}

// Ground a design document's FILE-reference citations against the cloned
// working tree before emitting, so the backend completion gate can reject an
// ungrounded design (one whose file citations all point at paths absent from
// the repo). The design agent already has the repo on disk, so this resolution
// is cheap and authoritative. Web-URL citations (http/https) are external and
// ignored here. A `file:` scheme prefix is stripped and a trailing `:line` /
// `#symbol` locator is dropped before the existence check; only path-like
// references (containing a `/`) are counted as file citations. Stamps a
// `_citation_grounding` summary {file_citations, grounded, checked, unresolved}
// onto the document in place and returns it. `unresolved` lists the specific
// file-reference citation strings that did NOT resolve to a real path — the
// agent's working tree is the only place that can know this, so it is captured
// here for the backend to persist as acceptance-probe detail (counts alone
// cannot say WHICH citations are absent). web_research never calls this, so its
// external URL citations are completely unaffected.
export function groundDesignCitations(
  document: Record<string, unknown>, workspace: string = WORKSPACE,
): Record<string, unknown> {
  const cites = Array.isArray(document.source_citations)
    ? document.source_citations
    : [];
  let fileCitations = 0;
  let grounded = 0;
  const unresolved: string[] = [];
  for (const raw of cites) {
    if (typeof raw !== 'string') continue;
    const text = raw.trim();
    if (!text || /^https?:\/\//i.test(text)) continue; // external URL — skip
    let ref = text.replace(/^file:/i, '').trim();
    ref = ref.split('#')[0].split(':')[0].trim(); // drop #symbol / :line locator
    if (!ref || !ref.includes('/')) continue; // not a path-like file reference
    fileCitations += 1;
    let resolved = false;
    try {
      if (fs.existsSync(path.join(workspace, ref))) resolved = true;
    } catch { /* unreadable path — treat as ungrounded */ }
    if (resolved) grounded += 1;
    else unresolved.push(text); // the offending citation, as written
  }
  document._citation_grounding = {
    file_citations: fileCitations, grounded, checked: true, unresolved,
  };
  return document;
}

// Cap how many unresolved citation strings are echoed back into the reopen
// prompt — a runaway citations array must not blow up the continuation prompt.
const MAX_UNRESOLVED_IN_PROMPT = 20;

// Focused continuation prompt for the citation-grounding repair reopen: echoes
// the unresolved citation strings exactly as the agent wrote them so it can see
// WHICH format/path it got wrong, and constrains the fix to the
// source_citations array only.
export function buildCitationRepairPrompt(
  unresolved: string[], documentPath: string,
): string {
  const shown = unresolved.slice(0, MAX_UNRESOLVED_IN_PROMPT);
  const more = unresolved.length - shown.length;
  const list = shown.map((c) => `- ${c}`).join('\n');
  return `Your design document was written, but NONE of its file citations resolve to real paths in the repository working tree, so the quality gate will DISCARD the document as ungrounded. The unresolved citations, exactly as you wrote them:

${list}${more > 0 ? `\n(+${more} more)` : ''}

Fix ONLY the source_citations array — keep title, summary, body, tags, and topic unchanged. Each file citation must be the repository-relative path of a real file you actually read, exactly as it exists on disk (verify each with ls): bare like backend/app/example.py or prefixed like file:backend/app/example.py. Do NOT wrap paths in backticks or quotes, do NOT append descriptions or line ranges, and do NOT use absolute paths.

Rewrite the FULL corrected document JSON to ${documentPath} (use Bash — the Write tool is disabled), and ALSO output the same complete JSON object as the VERY LAST thing in your response.`;
}

// When file citations are present but NONE resolve, the backend forward-quality
// gate (knowledge_completion._design_grounding_miss) discards the document
// wholesale. Instead of posting a doomed completion, reopen EXACTLY ONCE with
// the unresolved citation strings so the agent can correct the citation
// format/paths it actually read — feedback to the generating behavior, not a
// silent rewrite of its output. The repaired document is re-grounded and only
// adopted when at least one citation now resolves; otherwise the original
// (with its truthful zero-grounded stamp) is kept for the gate to judge.
async function repairUngroundedCitations(
  document: Record<string, unknown>, taskId: string, documentPath: string,
  ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string, disallowedTools?: string[],
): Promise<Record<string, unknown>> {
  const grounding = document._citation_grounding as
    { file_citations?: number; grounded?: number; unresolved?: unknown } | undefined;
  const fileCitations = grounding?.file_citations ?? 0;
  const grounded = grounding?.grounded ?? 0;
  if (fileCitations <= 0 || grounded > 0) return document;
  const unresolved = Array.isArray(grounding?.unresolved)
    ? grounding.unresolved.map((u) => String(u))
    : [];
  // The unresolved strings go to the VM log too — they are otherwise lost when
  // the backend gate fails the task (the fail path persists only the counts).
  sendLog(ws, `[WARNING] None of the ${fileCitations} file citation(s) resolve in the working tree (unresolved: ${unresolved.join(' | ')}) — reopening once so the agent can correct them\n`);
  statusRef.value = 'Correcting unresolved design citations...';
  const retry = await runClaudeCode(
    buildCitationRepairPrompt(unresolved, documentPath),
    ws, statusRef, model, effort, disallowedTools,
  );
  // Candidate order: the re-written /tmp document file first, then a document
  // object in the retry output. readHandlerOutput is non-consuming, so a retry
  // that never touched the file re-yields the original (still ungrounded)
  // document — which simply fails the grounded>0 adoption check below.
  const candidates: Record<string, unknown>[] = [];
  const fromFile = readHandlerOutput(taskId, 'design_document', ws);
  if (fromFile !== null && typeof fromFile === 'object' && !Array.isArray(fromFile)) {
    candidates.push(fromFile as Record<string, unknown>);
  }
  for (const text of [retry.lastResultText, retry.allOutput]) {
    try {
      const fromText = extractJsonObject(text || '', 'title', 'summary', 'body');
      if (fromText) {
        candidates.push(fromText);
        break;
      }
    } catch { /* malformed JSON in retry output — fall through */ }
  }
  for (const candidate of candidates) {
    // The repaired document must still satisfy the backend's structural gate.
    if (!strVal(candidate.title) || !strVal(candidate.summary) || !strVal(candidate.body)) continue;
    groundDesignCitations(candidate);
    const repairedGrounding = candidate._citation_grounding as { grounded?: number };
    if ((repairedGrounding.grounded ?? 0) > 0) {
      sendLog(ws, `[INFO] Citation repair succeeded: ${repairedGrounding.grounded} file citation(s) now resolve\n`);
      return candidate;
    }
  }
  sendLog(ws, '[WARNING] Citation repair did not resolve any file citations — keeping the original document\n');
  return document;
}

// Capture the git branch and short HEAD commit from the workspace. Returns an
// object with authoring_branch and head_sha fields, or an empty object if
// the workspace is not a git repository or git commands fail.
function captureGitProvenance(workspace: string = WORKSPACE): Record<string, string> {
  try {
    const branch = execSync('git rev-parse --abbrev-ref HEAD', { cwd: workspace }).toString().trim();
    const sha = execSync('git rev-parse --short HEAD', { cwd: workspace }).toString().trim();
    const result: Record<string, string> = {};
    if (branch) result.authoring_branch = branch;
    if (sha) result.head_sha = sha;
    return result;
  } catch {
    return {};
  }
}

// ── Handler: Design (codebase-internal design, terminal knowledge doc) ────────
//
// A design task takes a design question ("design how system X should work in
// THIS codebase") and produces a single structured knowledge DOCUMENT as JSON —
// title, summary, body (markdown), source_citations, tags, topic. Unlike
// web_research it grounds the design in the ACTUAL repo: it CLONES the repo
// (requires_code=True) but runs strictly read-only (no Write/Edit, no PR, no
// child spawning) and cites FILE references (file:path or path) as its default
// citations; web URLs remain acceptable. The backend completion handler
// (knowledge_completion._handle_design_completion) parses the document out of
// the posted `logs`, gates it through the forward-quality evaluator, and
// persists exactly one KnowledgeDocument row.
//
// Modeled on handleWebResearch for the output channel (a /tmp file read back via
// readHandlerOutput plus the same JSON in the completion logs) and on
// handleDesignDocUpdate for the read-only clone of the repo.
export async function handleDesign(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const prompt = strVal(payload.prompt) || strVal(payload.topic) || strVal(task.description);
  const topic = strVal(payload.topic);
  if (!prompt) throw new Error('Design task missing prompt');

  // Design grounds in the codebase — clone the repo, then install the read_only
  // permission posture (Write/Edit/NotebookEdit disallowed, git push blocked).
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  statusRef.value = `Cloning ${owner}/${repo} for design analysis...`;
  sendLog(ws, `Starting codebase-internal design...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: DEFAULT_CLONE_DEPTH,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  // The design prompt advertises `./design-doc get <key>` for architectural
  // grounding; provision the read-only helper so that contract is honest.
  writeDesignDocHelper(taskId, WORKSPACE, { readOnly: true });
  // Provision the read-only ./fetch helper (direction|section|playbook) so the
  // design agent can resolve refs trimmed from its launch snapshot. project_id is
  // set for all task types by the backend; if it is missing we skip BOTH the
  // helper AND its prompt advertisement so we never promise a helper we didn't write.
  const designProjectId = ((task.project_metadata as Record<string, unknown> | undefined)?.project_id as string | undefined) ?? '';
  if (designProjectId) writeFetchHelper(taskId, designProjectId, WORKSPACE, { verbs: PLAN_FETCH_VERBS });
  const designFetchGuidance = designProjectId ? buildFetchGuidance(PLAN_FETCH_VERBS) : '';

  const disallowedTools = configureAgentPermissions('design');

  const contextPrefix = buildContextPrefix(task);
  const retryContext = buildRetryContext(task);
  const designPlaybookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const topicLine = topic ? `Topic: ${topic}\n\n` : '';

  const documentPath = `/tmp/design_document_${taskId}.json`;
  const progressPath = `/tmp/design_progress_${taskId}.md`;

  // Pull the most relevant existing knowledge documents (topic preferred, else
  // the prompt) so the design builds on prior knowledge instead of re-deriving
  // from scratch and silently duplicating or contradicting an existing design.
  // Fail-soft: an empty list yields no section and no failure.
  const priorDocs = await fetchPriorKnowledge(topic || prompt);
  if (priorDocs.length > 0) {
    sendLog(ws, `Injecting ${priorDocs.length} prior knowledge document(s) into the design prompt\n`);
  }
  const priorKnowledgeSection = buildPriorKnowledgeSection(priorDocs);

  // Shared capability vocabulary — the canonical registry names the design should
  // reference (and the uniform provides_capability / prerequisite framing every
  // task type shares). Additive: '' when the payload carries no capabilities.
  const designVocabSection = buildAvailableCapabilitiesSection(
    payload.available_capabilities,
  );
  const designAvailableCapabilitiesSection = designVocabSection
    ? `${designVocabSection}${CAPABILITY_DECLARATION_DOC}\n\n`
    : '';

  const designPrompt = `${claudeMdPrefix}${designPlaybookSection}${retryContext}${contextPrefix}You are a software design agent. Your job is to answer the design question below by GROUNDING your design in the ACTUAL code of this repository, and produce a single structured design DOCUMENT. You operate strictly read-only: you do NOT edit files, you do NOT open a pull request, and you do NOT spawn any follow-up work.

${topicLine}Design question:
${prompt}

${priorKnowledgeSection}${designAvailableCapabilitiesSection}# HOW TO DESIGN
1. Explore the codebase THOROUGHLY — read the real modules, types, and call sites relevant to the question. Use subagents for parallel exploration.
2. Ground every design decision in what the code ACTUALLY does today. Reference concrete files and symbols; do not invent structure that isn't there.
3. Propose a concrete, reviewable design: the components involved, how they interact, the data/contract shapes, the trade-offs, and the migration/rollout path where relevant.
4. Track the actual files you grounded the design in — every substantive claim must be backed by a file-reference citation.

# OUTPUT — single design document
Write your document as a JSON object to ${documentPath} (use Bash to write the file — the Write tool is disabled for this read-only task). The file must contain ONLY the JSON object, no markdown wrapping. Schema:

{
  "title": "Concise design title (under ~120 chars)",
  "summary": "1-3 sentence summary of the proposed design.",
  "body": "The full design in markdown. Use headings, lists, and short paragraphs. Cover the current state, the proposed design, contracts/shapes, trade-offs, and rollout. This is the durable design artifact.",
  "source_citations": ["file:backend/app/example.py", "path/to/another.ts", "https://optional-external-ref"],
  "tags": ["short-tag-1", "short-tag-2"],
  "topic": "${topic || 'short topic slug'}"
}

Rules:
- title, summary, and body are REQUIRED and must be non-empty strings. body must be substantive markdown grounded in the code you read.
- source_citations is REQUIRED and must contain at least one usable citation. FILE references are the default — cite the actual files your design is grounded in (file:path or a path like backend/app/x.py). Web URLs (https://...) are also accepted. An ungrounded design with no usable citation FAILS the forward-quality gate and is discarded.
- file citations are MACHINE-CHECKED against the working tree: each must be a repository-relative path exactly as it exists on disk (verify with ls before emitting). Do NOT wrap paths in backticks or quotes, do NOT append descriptions or line ranges, and do NOT use absolute paths — a design whose file citations ALL fail to resolve is DISCARDED.
- tags and topic are optional but recommended; tags is a list of short strings.
- Also emit the same JSON object as the VERY LAST thing in your response (in addition to writing the file), so it can be recovered if the file write fails.

ALSO write ${progressPath} — a concise markdown summary (under ~3000 chars) of your analysis: which areas of the codebase you explored, the key files, the design direction, and any open questions. This is surfaced to retry agents if this task fails, so they can resume instead of re-analyzing.${promptSuffix}

## Codebase
${fileTreeSection}

Read full design-doc section prose on demand with \`./design-doc get <key>\` (list keys with \`./design-doc list\`).

${PLAYBOOK_GUIDANCE}

${designFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;

  statusRef.value = 'Running design analysis...';
  const fingerprint = computePromptFingerprint('design');
  setCurrentPromptFingerprint(fingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: fingerprint });

  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    designPrompt, ws, statusRef, model, effort, disallowedTools,
  );

  // Prefer reading the document from the /tmp file (avoids output truncation);
  // fall back to parsing a JSON object with the required document keys from the
  // final result text or the full transcript.
  let logs = '';
  let document: Record<string, unknown> | null = null;
  const documentFromFile = readHandlerOutput(taskId, 'design_document', ws);
  if (documentFromFile !== null && typeof documentFromFile === 'object' && !Array.isArray(documentFromFile)) {
    document = documentFromFile as Record<string, unknown>;
    sendLog(ws, `Read design document from ${documentPath}\n`);
  }
  const progress = readDesignProgress(taskId);
  if (!document) {
    const priorAnalysisSection = progress ? `${progress.slice(-8000)}\n\n` : '';
    const repair = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys: ['title', 'summary', 'body'],
      schemaLabel: 'design',
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `${priorAnalysisSection}You did not emit the design document JSON in the required schema. Output ONLY the JSON object now — no markdown, no explanation. Required fields: title, summary, body. Schema:\n\n{"title":"Concise design title","summary":"1-3 sentence summary.","body":"Full design in markdown.","source_citations":["file:backend/app/example.py"],"tags":["tag"],"topic":"slug"}\n\nOutput ONLY the JSON object.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Retrying design document extraction...',
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
    // Ground file-reference citations against the cloned working tree before
    // emitting so the backend gate can reject an ungrounded design.
    groundDesignCitations(document);
    if (exitCode === 0) {
      // Gate-failing grounding (citations present, zero resolve) gets one
      // bounded reopen so the agent can fix its citation format before the
      // completion posts. Skipped on a CLI crash — same setup, wasted slot.
      document = await repairUngroundedCitations(
        document, taskId, documentPath, ws, statusRef, model, effort, disallowedTools,
      );
    }
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
    sendLog(ws, `[ERROR] Failed to post design completion status: ${String(e)}\n`);
  }
}
