/**
 * Human-readable tooltip for each compute acceptance-grade reason code emitted by
 * the backend classifier (app.knowledge_query.classify_knowledge_acceptance). The
 * codes distinguish the strict acceptance path (`llm_artifact_cited`) from the
 * measured-only causes — a deterministic fallback summary, a grounding miss, or a
 * citation that never resolved to a durable artifact.
 */
export const ACCEPTANCE_REASON_LABELS: Record<string, string> = {
  llm_artifact_cited: 'LLM-grounded, artifact-cited analysis — qualifies as acceptance evidence',
  analysis_mode_not_llm: 'Deterministic fallback summary (analysis mode is not LLM) — measured only',
  citation_grounding_miss: 'Citation grounding miss — measured only',
  no_source_citations: 'No source citations — measured only',
  missing_grounding_probe: 'No grounding probe recorded — measured only',
  no_grounded_artifact_citation: 'No citation resolved to a durable artifact — measured only',
};
