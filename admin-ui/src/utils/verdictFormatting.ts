// Shared verdict formatting for the evaluation-summary contract
// (TaskEvaluationSummary.verdict). The backend emits lowercased machine tokens
// (e.g. `approve`, `request_changes`); operator-facing compact surfaces must
// render a human-readable label with consistent positive/blocking styling and
// must never leak the raw snake_case token. This is the single source of truth
// for that mapping — TaskCard, ResearchBrowser, and TaskDetailDrawerMeta all
// consume it so the styling stays in lockstep.

export type VerdictTone = 'positive' | 'blocking' | 'neutral' | 'info';

// Reuse the EXACT class strings already used by TaskDetailDrawerMeta's
// EvaluationSummarySection so the styling is byte-for-byte consistent.
const TONE_BADGE_CLASS: Record<VerdictTone, string> = {
  positive: 'bg-emerald-900/40 text-emerald-300 border-emerald-800/40',
  blocking: 'bg-red-900/40 text-red-300 border-red-800/40',
  neutral: 'bg-neutral-800/50 text-neutral-300 border-neutral-700/50',
  info: 'bg-cyan-900/40 text-cyan-300 border-cyan-800/40',
};

// Known verdict tokens (case-insensitive). `approve`/`pass`/`clean`/`success`
// read as positive; `request_changes`/`fail`/`actionable`/`failure` as
// blocking; `undetected` as neutral; everything else falls through to info.
const POSITIVE_VERDICTS = new Set(['approve', 'pass', 'clean', 'success']);
const BLOCKING_VERDICTS = new Set(['request_changes', 'fail', 'actionable', 'failure']);
const NEUTRAL_VERDICTS = new Set(['undetected']);

// Explicit human-readable labels for known tokens; unknown tokens fall back to
// Title Case (so a stray machine token still never renders as snake_case).
const VERDICT_LABELS: Record<string, string> = {
  approve: 'Approve',
  request_changes: 'Request Changes',
  pass: 'Pass',
  fail: 'Fail',
  actionable: 'Actionable',
  clean: 'Clean',
  undetected: 'Undetected',
  recorded: 'Recorded',
  success: 'Success',
  failure: 'Failure',
};

function titleCase(token: string): string {
  return token
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

export function verdictTone(verdict: string): VerdictTone {
  const token = (verdict ?? '').trim().toLowerCase();
  if (POSITIVE_VERDICTS.has(token)) return 'positive';
  if (BLOCKING_VERDICTS.has(token)) return 'blocking';
  if (NEUTRAL_VERDICTS.has(token)) return 'neutral';
  return 'info';
}

export function verdictToneBadgeClass(tone: VerdictTone): string {
  return TONE_BADGE_CLASS[tone];
}

export function formatVerdict(verdict: string): { label: string; tone: VerdictTone } {
  const raw = (verdict ?? '').trim();
  const token = raw.toLowerCase();
  const label = VERDICT_LABELS[token] ?? (raw ? titleCase(raw) : '');
  return { label, tone: verdictTone(raw) };
}

// Convenience: badge class straight from a verdict token.
export function verdictBadgeClass(verdict: string): string {
  return verdictToneBadgeClass(verdictTone(verdict));
}
