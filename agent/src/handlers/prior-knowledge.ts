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
