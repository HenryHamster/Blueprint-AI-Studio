import type { StrategicChallengeSummary } from '../types/digest';

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

  // Verdict → border/text/background. Approved is intentionally muted so a clean
  // challenge does not visually compete with genuine warnings.
  const tone =
    verdict === 'blocked'
      ? 'text-red-300 border-red-500/40 bg-red-500/10'
      : verdict === 'warned'
        ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
        : 'text-emerald-300 border-emerald-500/30 bg-emerald-500/10';

  const counts: string[] = [];
  if (blocked > 0) counts.push(`${blocked} blocked`);
  if (warned > 0) counts.push(`${warned} warned`);

  const mode = summary.mode ?? 'ok';
  const visibleLabel = `challenge: ${mode} · ${verdict}`;
  const title = `Strategic judgment challenge — ${mode} / ${verdict}`;

  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-semibold ${tone}`}
      title={title}
      aria-label={title}
    >
      <span>{visibleLabel}</span>
      {counts.length > 0 && <span className="font-normal opacity-90">· {counts.join(', ')}</span>}
    </span>
  );
}
