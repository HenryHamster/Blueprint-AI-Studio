import type { DigestData } from './DashboardHome';

const REVIEW_CAT_ORDER = ['lint', 'type', 'logic', 'scope', 'architecture', 'other'] as const;
const REVIEW_CAT_COLORS: Record<string, string> = {
  lint: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  type: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  logic: 'bg-red-500/20 text-red-400 border-red-500/30',
  scope: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  architecture: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  other: 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30',
};

export function ReviewRejectionBreakdown({ digest }: { digest: DigestData | null }) {
  const cats = digest?.first_attempt_success_rate?.first_attempt_by_review_category;
  if (!cats || Object.keys(cats).length === 0) return null;
  const ordered = REVIEW_CAT_ORDER.filter(k => cats[k] != null);
  const extras = Object.keys(cats).filter(k => !REVIEW_CAT_ORDER.includes(k as typeof REVIEW_CAT_ORDER[number]));
  const allKeys = [...ordered, ...extras];
  return (
    <section>
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-ui-sm text-neutral-500 uppercase tracking-wide mr-1">Review Rejection</span>
        {allKeys.map(k => {
          const pct = cats[k];
          if (pct == null) return null;
          const color = REVIEW_CAT_COLORS[k] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
          return (
            <span key={k} className={`inline-flex items-center gap-1 text-ui-sm font-semibold px-2 py-0.5 rounded border ${color}`} title={`${k}: ${Math.round(pct * 100)}% of first-attempt rejections`}>
              {k}
              <span className="font-bold">{Math.round(pct * 100)}%</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}
