// Canonical semantic status tones and their token-backed Tailwind badge
// classes. Shared by the status/verdict/task badge helpers (ui/Badge.tsx,
// utils/taskStatus.ts, utils/verdictFormatting.ts) so every badge resolves
// through a single tone→class mapper and can never drift apart.
//
// (The legacy per-project lifecycle badge helpers — projectStatusBadge /
// isProjectActive — were removed: Project.status is a deprecated finite-project
// concept and is no longer surfaced as operator-facing state.)

/** Semantic tone for status/verdict badges — maps to @theme status tokens. */
export type StatusTone = 'success' | 'blocking' | 'warning' | 'info' | 'neutral';

const STATUS_TONE_CLASSES: Record<StatusTone, string> = {
  success:  'bg-status-success/15 text-status-success border border-status-success/20',
  blocking: 'bg-status-blocking/15 text-status-blocking border border-status-blocking/20',
  warning:  'bg-status-warning/15 text-status-warning border border-status-warning/20',
  info:     'bg-status-info/15 text-status-info border border-status-info/20',
  neutral:  'bg-status-neutral/15 text-status-neutral border border-status-neutral/20',
};

/** Canonical token-backed badge class string for a given semantic tone. */
export function statusBadgeClasses(tone: StatusTone): string {
  return STATUS_TONE_CLASSES[tone];
}
