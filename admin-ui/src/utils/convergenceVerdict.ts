// Shared extraction of the review/fix convergence verdict the backend bakes
// into `narrative_summary` (see digest_metrics.compute_narrative_summary). The
// sentence always starts with "Review/fix churn is" and runs to the next
// period. Both SystemStatusCard and CatchUpNarrativeSummary lift it out of the
// prose so the headline churn verdict reads as its own emphasized line; keeping
// the regex and split logic in one place ensures the two surfaces stay in
// lockstep (they can render for the same digest simultaneously). On thin windows
// the backend emits no such sentence, so nothing extra is surfaced.
export const CONVERGENCE_VERDICT_RE = /Review\/fix churn is[^.]*\./;

export interface ConvergenceVerdictSplit {
  /** The lifted verdict sentence, or null when the narrative carries none. */
  verdict: string | null;
  /** The narrative with the verdict sentence removed and whitespace collapsed. */
  prose: string;
}

/**
 * Split a narrative string into its convergence verdict (if any) and the
 * remaining prose. When no verdict is present, `verdict` is null and `prose` is
 * the narrative unchanged.
 */
export function splitConvergenceVerdict(narrative: string | null | undefined): ConvergenceVerdictSplit {
  const text = narrative ?? '';
  const verdict = text.match(CONVERGENCE_VERDICT_RE)?.[0] ?? null;
  const prose = verdict
    ? text.replace(CONVERGENCE_VERDICT_RE, '').replace(/\s{2,}/g, ' ').trim()
    : text;
  return { verdict, prose };
}
