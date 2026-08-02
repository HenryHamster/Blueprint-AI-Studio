// Review-history formatting helpers extracted from review-fix.ts.
// These are pure functions with no I/O dependencies.

export type ReviewHistoryIssue = {
  issue_id?: string;
  severity: string;
  description: string;
  file?: string;
  line?: number;
};
export type IssueResolution = {
  issue_id?: string;
  disposition?: 'fixed' | 'refuted' | 'adjudicated';
  reason?: string;
};
export type ReviewHistoryEntry = {
  iteration: number;
  feedback: string;
  issues?: ReviewHistoryIssue[];
  verdict?: 'APPROVE' | 'REQUEST_CHANGES';
  changed_files?: string[];
  reviewed_head_sha?: string;
  resolutions?: IssueResolution[];
};

type DriftInEntry = {
  file: string;
  first_appeared_iteration?: number;
  flag_count?: number;
};
export type DriftSignal = { drift_in?: DriftInEntry[]; drift_out?: string[] };

// Skepticism gate: a structured fix-agent response is only kept if it has at
// least one mechanical verify step. "Trust me", "I'm confident", "I tested
// it" are sycophancy patterns that turn the reviewer into a judge of rhetoric
// rather than a verifier. Mechanical steps reference commands, files, or
// greppable assertions. Heuristic — false positives here are cheap (one
// extra reviewer round trip), false negatives are expensive (sycophancy slips
// through), so the heuristic leans permissive on what counts as mechanical.
const NON_MECHANICAL_VERIFY_PATTERNS = [
  /\btrust me\b/i,
  /\bi tested\b/i,
  /\bi'?m confident\b/i,
  /\bbelieve me\b/i,
  /\btake my word\b/i,
];
export function verifyStepsOk(entry: Record<string, unknown>): boolean {
  const claim = typeof entry.claim === 'string' ? entry.claim.trim() : '';
  if (claim.length === 0) return false;
  const verify = Array.isArray(entry.verify) ? entry.verify : [];
  if (verify.length === 0) return false;
  const stringSteps = verify.filter((s): s is string => typeof s === 'string' && s.trim().length > 0);
  if (stringSteps.length === 0) return false;
  // Reject if EVERY verify step is non-mechanical. A mix is OK — the reviewer
  // can pick the actionable ones. This way honest agents who pad with
  // "I also re-read the code" still get their verifiable steps respected.
  const allNonMechanical = stringSteps.every(step =>
    NON_MECHANICAL_VERIFY_PATTERNS.some(p => p.test(step)),
  );
  return !allNonMechanical;
}

// Render a trajectory section showing each iteration's verdict and changed-file
// signature. Empty when no entries carry verdict info (older backends that
// hadn't shipped the trajectory plumbing). Surfaces approved iterations
// alongside REQUEST_CHANGES — the load-bearing signal for cases like PR #3435
// where iter 1 was approved but the test changes got lost in iter 2's rebase.
export function formatTrajectorySection(history: ReviewHistoryEntry[]): string {
  if (history.length === 0) return '';
  const hasVerdicts = history.some(e => e.verdict === 'APPROVE' || e.verdict === 'REQUEST_CHANGES');
  if (!hasVerdicts) return '';
  const sorted = [...history].sort((a, b) => a.iteration - b.iteration);
  const lines = sorted.map(e => {
    const verdict = e.verdict || 'REQUEST_CHANGES';
    const filesPart = e.changed_files && e.changed_files.length > 0
      ? ` — diff: ${e.changed_files.slice(0, 8).join(', ')}${e.changed_files.length > 8 ? `, +${e.changed_files.length - 8} more` : ''}`
      : '';
    return `  iter ${e.iteration}: ${verdict}${filesPart}`;
  });
  return `## PR trajectory\n${lines.join('\n')}\n\n`;
}

// Render the drift signal section using fields computed by the orchestrator
// at fix-spawn time (review_pipeline.compute_drift_signal). Informational —
// the agent decides whether to revert, justify, or expand scope.
export function formatDriftSection(drift: DriftSignal | undefined): string {
  if (!drift) return '';
  const driftIn = drift.drift_in || [];
  const driftOut = drift.drift_out || [];
  if (driftIn.length === 0 && driftOut.length === 0) return '';
  const inLines = driftIn.map(d => {
    const meta: string[] = [];
    if (typeof d.first_appeared_iteration === 'number') {
      meta.push(`first appeared iter ${d.first_appeared_iteration}`);
    }
    if (typeof d.flag_count === 'number' && d.flag_count > 0) {
      meta.push(`flagged ${d.flag_count}x`);
    }
    const metaStr = meta.length > 0 ? `    (${meta.join('; ')})` : '';
    return `  - ${d.file}${metaStr}`;
  });
  const outLines = driftOut.map(f => `  - ${f}`);
  let body = '## Drift signal (informational — not a directive)\n';
  if (inLines.length > 0) {
    body += `Files in current diff but NOT in {planned ∪ any-approved-iteration}:\n${inLines.join('\n')}\n\n`;
  }
  if (outLines.length > 0) {
    body += `Files expected (planned or approved before) but missing from current diff:\n${outLines.join('\n')}\n\n`;
  }
  body +=
    'Reading the trajectory: a file that drifted in during a rebase or earlier fix, ' +
    "has been flagged for removal repeatedly, and isn't justified by caller-impact / " +
    'discovered-dep / cluster-boundary correction → reverting via `git checkout origin/<base> -- <file>` ' +
    '(or `git rm` if newly added) usually breaks the loop.\n\n' +
    'But scope expansion IS sometimes the right call. If the planned files are genuinely insufficient ' +
    "and a sibling cluster's dependency or a discovered caller forces a wider edit, keep the change " +
    'AND record a structured response (see "Responding to reviewer issues" below). The reviewer accepts ' +
    'caller-impact / discovered-dependency / cluster-boundary corrections — your job is to distinguish ' +
    'those from leftover drift, not to reflexively revert.\n\n';
  return body;
}

// Mirrors backend _max_review_iterations (review_pipeline.py): ci_failure=8,
// small=10, medium=12, else=16. Keep in sync — if this drifts, the agent will
// claim "FINAL attempt" at the wrong iteration and collapse targeted fixes
// into one risky rewrite. Backend defaults missing scope to 'medium', so we
// do the same here (a bare task with no context still maps to 12, not 16).
export function computeMaxFixIterations(task: Record<string, unknown>): number {
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  const scope = (ctx.scope as string | undefined) || 'medium';
  const reviewLoop = ctx.review_loop as Record<string, unknown> | undefined;
  // Backend may pass an effective cap (reduced for low-quality directions). Prefer
  // it so the agent's "iteration N of M" and FINAL-attempt warning match reality.
  const backendMax = reviewLoop?.max_iterations;
  if (typeof backendMax === 'number' && Number.isFinite(backendMax) && backendMax > 0) {
    return Math.floor(backendMax);
  }
  const fixReason = reviewLoop?.fix_reason as string | undefined;
  if (fixReason === 'ci_failure') return 8;
  if (scope === 'small') return 10;
  if (scope === 'medium') return 12;
  return 16;
}
