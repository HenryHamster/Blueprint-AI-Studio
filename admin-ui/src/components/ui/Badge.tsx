import { verdictToneBadgeClass, formatVerdict } from '../../utils/verdictFormatting';

type VerdictTone = Parameters<typeof verdictToneBadgeClass>[0];

const BADGE_BASE = 'inline-flex items-center text-label font-medium border px-1.5 py-0.5 rounded';

/** A badge chip styled by semantic tone. */
export function StatusToneBadge({ tone, label }: { tone: VerdictTone; label: string }) {
  return <span className={`${BADGE_BASE} ${verdictToneBadgeClass(tone)}`}>{label}</span>;
}

/** Renders a raw verdict string as a styled badge (label + tone via verdictFormatting). */
export function VerdictBadge({ verdict }: { verdict: string }) {
  const { label, tone } = formatVerdict(verdict);
  return <StatusToneBadge tone={tone} label={label} />;
}
