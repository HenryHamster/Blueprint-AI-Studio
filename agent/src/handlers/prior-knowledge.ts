import { strVal, apiGet } from '../index.js';

// A single knowledge-document summary as returned by the agent-facing list
// endpoint (GET /api/agent/knowledge-documents). The list shape omits the
// markdown body; only id/title/summary/topic/tags are present.
export interface PriorKnowledgeDoc {
  id: string;
  title: string;
  summary: string;
}

// Cap on injected prior-knowledge docs — keep the prompt focused on the most
// relevant handful rather than dumping the whole store.
export const PRIOR_KNOWLEDGE_LIMIT = 5;

// Fetch the most relevant existing knowledge documents for this task using the
// task's topic/prompt as the full-text search query, via the agent's
// authenticated backend client (apiGet attaches the X-Agent-Token). Uses the
// existing trigram/ILIKE full-text search (the `q` term) — no embeddings.
//
// Fail-soft by contract: any error, timeout, or empty result returns [] so the
// caller proceeds exactly as it would with no prior knowledge. A knowledge
// fetch must NEVER break a research/design run.
export async function fetchPriorKnowledge(queryText: string): Promise<PriorKnowledgeDoc[]> {
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

// A single commissioned-prerequisite pointer as stamped onto a dependent task's
// `context.knowledge_prerequisites` by the backend when the prerequisite
// (web_research / design) completes. The handler resolves `doc_id` to the full
// document body at launch; title/summary are the durable fallback when the body
// cannot be resolved.
export interface PrerequisiteKnowledgePointer {
  doc_id: string;
  source_task_id?: string;
  source_kind?: string;
  title?: string;
  summary?: string;
}

// The full knowledge document as returned by the agent by-id endpoint
// (GET /api/agent/knowledge-documents/{id}), which includes the markdown body.
export interface PrereqKnowledgeDoc {
  id: string;
  title: string;
  summary: string;
  body: string;
}

export interface PrerequisiteArtifactPointer {
  artifact_id: string;
  logical_key?: string;
  artifact_type?: string;
  source_task_id?: string;
  source_compute_job_id?: string;
  storage_uri?: string;
}

export async function fetchArtifactDownloadUrl(artifactId: string): Promise<string | null> {
  const id = (artifactId || '').trim();
  if (!id) return null;
  try {
    const path = `/api/agent/artifacts/download-url?artifact_id=${encodeURIComponent(id)}`;
    const d = await apiGet(path) as Record<string, unknown> | null;
    if (!d || typeof d !== 'object') return null;
    return strVal(d.url) || null;
  } catch {
    return null;
  }
}

// Resolve a single knowledge document (including its markdown body) by id via the
// agent's authenticated backend client. Fail-soft by contract: any error, timeout,
// 404, or malformed response returns null so a resolution miss never breaks the
// dependent task's run — the caller falls back to the stamped title/summary.
export async function fetchKnowledgeDocumentById(id: string): Promise<PrereqKnowledgeDoc | null> {
  const docId = (id || '').trim();
  if (!docId) return null;
  try {
    const path = `/api/agent/knowledge-documents/${encodeURIComponent(docId)}`;
    const d = await apiGet(path) as Record<string, unknown> | null;
    if (!d || typeof d !== 'object') return null;
    const resolvedId = strVal(d.id);
    if (!resolvedId) return null;
    return {
      id: resolvedId,
      title: strVal(d.title),
      summary: strVal(d.summary),
      body: strVal(d.body),
    };
  } catch {
    return null;
  }
}

// Build a `## Commissioned Prerequisite Knowledge` section from the pointers a
// plan stamped onto this task and the bodies resolved at launch (keyed by doc id).
// Framed as knowledge produced SPECIFICALLY to inform this task — build on it.
// Falls back to the stamped title/summary when a body could not be resolved, so a
// resolution miss degrades to less context rather than no section. Returns '' when
// there are no prerequisites so the prompt is byte-for-byte unchanged otherwise.
export function buildPrerequisiteKnowledgeSection(
  pointers: PrerequisiteKnowledgePointer[],
  resolved: Map<string, PrereqKnowledgeDoc>,
): string {
  if (pointers.length === 0) return '';
  const blocks: string[] = [];
  for (const p of pointers) {
    const docId = (p.doc_id || '').trim();
    if (!docId) continue;
    const doc = resolved.get(docId);
    const title = (doc?.title || p.title || '').trim() || '(untitled)';
    const header = `### [${docId}] ${title}`;
    if (doc && doc.body.trim()) {
      blocks.push(`${header}\n\n${doc.body.trim()}`);
    } else {
      const summary = (doc?.summary || p.summary || '').trim() || '(no summary available)';
      blocks.push(`${header}\n\n${summary}\n\n_(full document body could not be resolved — building on the summary above)_`);
    }
  }
  if (blocks.length === 0) return '';
  return `## Commissioned Prerequisite Knowledge
The following knowledge was produced SPECIFICALLY to inform this task: a plan commissioned this research/design as a prerequisite and made this task depend on it. Treat it as authoritative input — build directly ON it rather than re-deriving or re-researching from scratch, and cite the relevant document id(s) where your work uses or extends it.

${blocks.join('\n\n')}

`;
}

export function buildPrerequisiteArtifactSection(
  pointers: PrerequisiteArtifactPointer[],
  signedUrls: Map<string, string>,
  taskId: string,
  localPaths: Map<string, string> = new Map(),
): string {
  if (pointers.length === 0) return '';
  const entries = pointers
    .filter((p) => (p.artifact_id || '').trim())
    .map((p) => {
      const id = p.artifact_id.trim();
      const label = (p.logical_key || p.artifact_type || 'artifact').trim();
      const url = signedUrls.get(id);
      const source = p.source_task_id ? ` from task ${p.source_task_id}` : '';
      const localPath = localPaths.get(id);
      const link = url && !localPath ? `\n  signed_download_url: ${url}` : '';
      const local = localPath
        ? `\n  local_path: ${localPath}/\n  downloaded_archive: ${localPath}/bundle.tar.gz\n  extracted_contents: ${localPath}/contents/`
        : '';
      return `- ${label} [artifact_id: ${id}]${source}${local}${link}`;
    });
  if (entries.length === 0) return '';
  const consumedFile = `/tmp/consumed_artifacts_${taskId}.json`;
  return `## Commissioned Prerequisite Artifacts
The following artifacts were produced SPECIFICALLY for this task by direct dependencies. Use the signed URL when present, cite the artifact_id(s) your work actually relies on, and only record artifact ids you truly consumed. When you finish: write ${consumedFile} as a JSON array of the artifact_id strings you actually consumed (e.g. ["id1","id2"]). If you used none, write an empty array [] to that file — do NOT omit the file, because omitting triggers a legacy fallback that over-credits all delivered artifacts as consumed.

When a local_path is present, the exact downloaded archive is at downloaded_archive and a bounded, traversal-safe extracted view is at extracted_contents. Read them directly; this is an ignored runtime input and must not be staged or committed wholesale. Use the signed URL only as a fallback.

${entries.join('\n')}

`;
}

// Drop prior-knowledge docs whose id already appears in a caller-supplied set.
// Used to prevent the same document appearing twice — once in the payload-built
// `## Existing knowledge documents` index and again in `## Prior Knowledge`.
// Pure function: never modifies `docs` in place. Returns all docs when
// `knownIds` is empty so the no-payload-index case is a zero-cost no-op.
export function filterKnowledgeDuplicates(docs: PriorKnowledgeDoc[], knownIds: Set<string>): PriorKnowledgeDoc[] {
  if (knownIds.size === 0) return docs;
  return docs.filter(d => !knownIds.has(d.id));
}

// Build a clearly delimited `## Prior Knowledge` section listing each retrieved
// document's title, summary, and id, with an explicit instruction to build on /
// extend / supersede the prior knowledge and cite the relevant document ids
// rather than re-deriving from scratch. Returns '' when there are no documents
// so the prompt is byte-for-byte unchanged in the no-knowledge case.
export function buildPriorKnowledgeSection(docs: PriorKnowledgeDoc[]): string {
  if (docs.length === 0) return '';
  const entries = docs
    .map((d) => `- [${d.id}] ${d.title}\n  ${d.summary || '(no summary)'}`)
    .join('\n');
  return `## Prior Knowledge
The knowledge store already holds the following related document(s) for this project. Build ON this prior knowledge: extend, refine, or supersede it rather than re-deriving from scratch, and cite the relevant document id(s) (e.g. "supersedes [<id>]") where your findings update or contradict them.

${entries}

`;
}
