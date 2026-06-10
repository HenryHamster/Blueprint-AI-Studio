const COMPUTE_ANALYSIS_CAUSE_LABELS: Record<string, string> = {
  'precondition-not-met': 'Missing credentials — analysis skipped',
  'analysis-disabled': 'Analysis disabled',
  'no-evidence': 'No evidence to analyze',
  'llm-error': 'Backend / LLM error',
  'citations-ungrounded': 'Ungrounded citations',
  analysis_mode_not_llm: 'Deterministic fallback summary (analysis mode is not LLM) — measured only',
};

function normalizeReason(value: string | null | undefined): string | null {
  const trimmed = typeof value === 'string' ? value.trim() : '';
  return trimmed.length > 0 ? trimmed : null;
}

function humanizeCause(reason: string): string {
  return reason
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\w/, (c) => c.toUpperCase());
}

export function formatComputeAnalysisCauseLabel(details: {
  unavailableCause?: string | null;
  analysisFallbackReason?: string | null;
  analysisMode?: string | null;
}): string | null {
  const unavailableCause = normalizeReason(details.unavailableCause);
  if (unavailableCause) {
    return COMPUTE_ANALYSIS_CAUSE_LABELS[unavailableCause] ?? humanizeCause(unavailableCause);
  }

  const fallbackReason = normalizeReason(details.analysisFallbackReason);
  if (fallbackReason) {
    return COMPUTE_ANALYSIS_CAUSE_LABELS[fallbackReason] ?? humanizeCause(fallbackReason);
  }

  const analysisMode = normalizeReason(details.analysisMode)?.toLowerCase();
  if (!analysisMode || analysisMode === 'llm') return null;
  if (analysisMode === 'fallback' || analysisMode === 'deterministic') {
    return COMPUTE_ANALYSIS_CAUSE_LABELS.analysis_mode_not_llm;
  }
  return `Analysis mode: ${analysisMode}`;
}
