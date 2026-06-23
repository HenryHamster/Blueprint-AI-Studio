import type { StrategicChallengeSummary } from '../types/digest';
import { Badge } from './ui/Badge';
import type { StatusTone } from './ui/Badge';

const VERDICT_TONE: Record<string, StatusTone> = {
  blocked: 'blocking',
  warned: 'warning',
  approved: 'success',
};

/**
 * Compact, read-only chip summarising a strategic-judgment challenge outcome.
 *
 * Renders ONLY when a summary object exists — cycles that predate challenge
 * records (and therefore carry no summary) get `null`, so absence is never
 * styled as a warning/failure. The verdict drives the color: an approved
 * challenge reads as a calm confirmation, warned as amber, blocked as red.
 * Warned/blocked counts are shown when non-zero so an operator sees the volume
 * of disputed judgments at a glance without the full item list being shipped.
 *
 * Centralises the verdict→style mapping so all three surfaces (completion
 * receipt, latest-cycle line, digest card) stay visually consistent.
 */
export function StrategicChallengeChip({
  summary,
}: {
  summary?: StrategicChallengeSummary | null;
}) {
  if (!summary) return null;

  const verdict = summary.verdict ?? 'approved';
  const warned = summary.warned_count ?? 0;
  const blocked = summary.blocked_count ?? 0;
  const tone: StatusTone = VERDICT_TONE[verdict] ?? 'success';

  const counts: string[] = [];
  if (blocked > 0) counts.push(`${blocked} blocked`);
  if (warned > 0) counts.push(`${warned} warned`);

  const mode = summary.mode ?? 'ok';
  const visibleLabel = `challenge: ${mode} · ${verdict}`;
  const title = `Strategic judgment challenge — ${mode} / ${verdict}`;

  return (
    <Badge tone={tone} title={title} aria-label={title} className="gap-1 font-semibold">
      <span>{visibleLabel}</span>
      {counts.length > 0 && <span className="font-normal opacity-90">· {counts.join(', ')}</span>}
    </Badge>
  );
}
