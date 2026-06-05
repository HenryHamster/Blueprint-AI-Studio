// Shared formatter for the no-CI merge gate's derived at-a-glance summary. Used
// by BOTH operator surfaces that render merge-verification health — the
// MergeVerificationBanner (visible line) and the ProjectDashboard merge-gate
// badge (tooltip append) — so the wording can never drift between them.
//
// Reuses the single-sourced humanizeUnverifiedReason label map (do NOT add a
// second reason-label map here).
import type { MergeGateSummary } from '../types/project';
import { humanizeUnverifiedReason } from './unverifiedReasonLabels';

/**
 * Build an operator-facing sentence like
 *   `verified 4 of the last 6 merges; fell open 2 times (most common: <label>)`
 * from the backend-derived merge-gate summary.
 *
 * Returns `null` when there is nothing meaningful to show — an absent summary,
 * or an empty window — so callers can render the line only when present and
 * non-trivial (legacy/never-ran records render exactly as before).
 *
 * The `(most common: …)` clause is omitted when there were no fall-opens or the
 * most-common reason key is absent.
 */
export function formatMergeGateSummary(summary?: MergeGateSummary | null): string | null {
  if (!summary) return null;
  const window = summary.window;
  if (typeof window !== 'number' || window <= 0) return null;

  const verified = summary.verified_runs ?? 0;
  const fellOpen = summary.fell_open_runs ?? 0;

  let sentence = `verified ${verified} of the last ${window} merge${window === 1 ? '' : 's'}`;
  if (fellOpen > 0) {
    sentence += `; fell open ${fellOpen} time${fellOpen === 1 ? '' : 's'}`;
    if (summary.most_common_reason) {
      sentence += ` (most common: ${humanizeUnverifiedReason(summary.most_common_reason)})`;
    }
  }
  return sentence;
}
