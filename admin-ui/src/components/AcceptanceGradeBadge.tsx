import { CheckCircle, HelpCircle } from 'lucide-react';
import type { KnowledgeDocument } from '../api';
import { formatComputeAnalysisCauseLabel } from '../utils/computeAnalysisLabels';
import { ACCEPTANCE_REASON_LABELS } from './acceptanceGradeLabels';

/**
 * Acceptance-grade badge for compute_experiment knowledge documents. The backend
 * (classify_knowledge_acceptance) is the single source of truth: it grades a
 * compute KnowledgeDocument as 'acceptance' (LLM-grounded, artifact-cited analysis
 * that clears the grounding probe) or 'measured_only' (deterministic fallback,
 * grounding miss, or unresolved citations). Operators need to tell the two apart
 * because only the acceptance grade qualifies as strict non-code acceptance
 * evidence. Renders nothing for non-compute documents (grade is null) or older
 * docs persisted before the grade existed, so measured-only/fallback docs stay
 * visible in normal search and source-kind filtering — this only labels them.
 */
export function AcceptanceGradeBadge({
  doc,
}: {
  doc: Pick<
    KnowledgeDocument,
    'source_kind' | 'compute_acceptance_grade' | 'compute_acceptance_reason' | 'analysis_fallback_reason'
    | 'analysis_mode' | 'unavailable_cause'
  >;
}) {
  if (doc.source_kind !== 'compute_experiment') return null;
  const grade = doc.compute_acceptance_grade;
  if (grade !== 'acceptance' && grade !== 'measured_only') return null;
  const isAcceptance = grade === 'acceptance';
  const reason = typeof doc.compute_acceptance_reason === 'string' ? doc.compute_acceptance_reason : undefined;
  const analysisCauseLabel = grade === 'measured_only'
    ? formatComputeAnalysisCauseLabel({
        unavailableCause: doc.unavailable_cause,
        analysisFallbackReason: doc.analysis_fallback_reason,
        analysisMode: doc.analysis_mode,
      })
    : null;
  const tooltip = (reason ? ACCEPTANCE_REASON_LABELS[reason] : undefined)
    ?? (isAcceptance ? 'Acceptance-grade compute knowledge' : 'Measured-only compute knowledge');
  const label = isAcceptance ? 'Acceptance' : 'Measured only';
  const cls = isAcceptance
    ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300'
    : 'bg-amber-500/10 border-amber-500/20 text-amber-300';
  return (
    <span className="inline-flex items-center gap-1 flex-wrap">
      <span
        className={`${cls} border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
        title={analysisCauseLabel ? `${tooltip} · ${analysisCauseLabel}` : tooltip}
      >
        {isAcceptance ? <CheckCircle className="w-2.5 h-2.5 shrink-0" /> : <HelpCircle className="w-2.5 h-2.5 shrink-0" />}
        {label}
      </span>
      {analysisCauseLabel && (
        <span className="text-ui-xs text-amber-200/80 leading-tight max-w-[14rem] break-words">
          {analysisCauseLabel}
        </span>
      )}
    </span>
  );
}
