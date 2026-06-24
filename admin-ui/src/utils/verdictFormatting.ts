// Shared verdict formatting for the evaluation-summary contract
// (TaskEvaluationSummary.verdict). The backend emits lowercased machine tokens
// (e.g. `approve`, `request_changes`); operator-facing compact surfaces must
// render a human-readable label with consistent positive/blocking styling and
// must never leak the raw snake_case token. This is the single source of truth
// for that mapping — TaskCard, ResearchBrowser, and TaskDetailDrawerMeta all
// consume it so the styling stays in lockstep.

import { statusBadgeClasses, type StatusTone } from './projectStatus';

type VerdictTone = 'positive' | 'blocking' | 'neutral' | 'info';

// Maps verdict tones to the canonical StatusTone so both verdict and project
// badges resolve through the single token-backed mapper in projectStatus.ts.
const VERDICT_TO_STATUS_TONE: Record<VerdictTone, StatusTone> = {
  positive: 'success',
  blocking: 'blocking',
  neutral:  'neutral',
  info:     'info',
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

function verdictTone(verdict: string): VerdictTone {
  const token = (verdict ?? '').trim().toLowerCase();
  if (POSITIVE_VERDICTS.has(token)) return 'positive';
  if (BLOCKING_VERDICTS.has(token)) return 'blocking';
  if (NEUTRAL_VERDICTS.has(token)) return 'neutral';
  return 'info';
}

export function verdictToneBadgeClass(tone: VerdictTone): string {
  return statusBadgeClasses(VERDICT_TO_STATUS_TONE[tone]);
}

export function formatVerdict(verdict: string): { label: string; tone: VerdictTone } {
  const raw = (verdict ?? '').trim();
  const token = raw.toLowerCase();
  const label = VERDICT_LABELS[token] ?? (raw ? titleCase(raw) : '');
  return { label, tone: verdictTone(raw) };
}
