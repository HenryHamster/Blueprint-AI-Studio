import { CheckCircle, XCircle, HelpCircle } from 'lucide-react';
import type { KnowledgeDocument } from '../api';

/**
 * Criterion verdict badge for compute_experiment knowledge documents. Displays
 * whether the declared success criterion was MET, NOT MET, or UNMEASURED.
 * The criterion_met field is tri-state (true/false/null), distinct from the
 * compute_acceptance_grade (trustworthiness signal).
 * Renders nothing for non-compute documents or when criterion_met is absent.
 */
export function CriterionVerdictBadge({
  doc,
}: {
  doc: Pick<KnowledgeDocument, 'source_kind' | 'criterion_met'>;
}) {
  if (doc.source_kind !== 'compute_experiment') return null;
  const verdict = doc.criterion_met;
  if (verdict === undefined) return null;

  if (verdict === true) {
    return (
      <span
        className="inline-flex items-center gap-1 bg-emerald-500/10 border-emerald-500/20 border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap px-1.5 py-0.5"
        title="Success criterion was MET"
      >
        <CheckCircle className="w-2.5 h-2.5 shrink-0 text-emerald-400" />
        <span className="text-emerald-300">Criterion MET</span>
      </span>
    );
  }

  if (verdict === false) {
    return (
      <span
        className="inline-flex items-center gap-1 bg-red-500/10 border-red-500/20 border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap px-1.5 py-0.5"
        title="Success criterion was NOT MET"
      >
        <XCircle className="w-2.5 h-2.5 shrink-0 text-red-400" />
        <span className="text-red-300">Criterion NOT MET</span>
      </span>
    );
  }

  // verdict === null means unmeasured/not evaluable
  return (
    <span
      className="inline-flex items-center gap-1 bg-amber-500/10 border-amber-500/20 border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap px-1.5 py-0.5"
      title="Success criterion was not measured or evaluable"
    >
      <HelpCircle className="w-2.5 h-2.5 shrink-0 text-amber-400" />
      <span className="text-amber-300">Criterion UNMEASURED</span>
    </span>
  );
}
