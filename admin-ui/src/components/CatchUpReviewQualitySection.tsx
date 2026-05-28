import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { SrTrendBreakdown, type SrTrendData } from './SrTrendBreakdown';

interface SelfReviewEffectiveness {
  with_self_review_approval_rate: number | null;
  without_self_review_approval_rate: number | null;
  clean_verdict_approval_rate: number | null;
  fixed_verdict_approval_rate: number | null;
  sample_size: number;
}

interface SelfReviewPredictionAccuracy {
  prediction_hit_rate: number | null;
  miss_rate: number | null;
  false_alarm_rate: number | null;
  sample_size: number;
}

interface SelfReviewSkipRate {
  overall_rate: number;
  skipped_count: number;
  sample_size: number;
  by_task_type: Record<string, { rate: number; skipped_count: number; sample_size: number }>;
}

interface ReviewAccuracy {
  total_reviews: number;
  overrides: number;
  dismissals: number;
  misses: number;
  total_approvals: number;
  precision: number | null;
  recall: number | null;
}

interface FirstReviewApprovalRate {
  overall: number | null;
  sample_size: number;
  by_type?: Record<string, number> | null;
  by_scope?: Record<string, number> | null;
  with_playbook?: number | null;
  without_playbook?: number | null;
  with_measurements?: number | null;
  without_measurements?: number | null;
}

interface FreshReviewerEffectiveness {
  fresh_reviewer_sample_size: number;
  fresh_reviewer_first_attempt_approval_rate: number | null;
  baseline_first_attempt_approval_rate: number | null;
  per_category_hit_rate: Record<string, number>;
  reviewer_finding_added_value: Record<string, number>;
  prediction_sample_size: number;
}

interface CatchUpReviewQualitySectionProps {
  selfReviewEffectiveness?: SelfReviewEffectiveness | null;
  selfReviewPredictionAccuracy?: SelfReviewPredictionAccuracy | null;
  selfReviewPredictionAccuracyTrends?: SrTrendData | null;
  selfReviewSkipRate?: SelfReviewSkipRate | null;
  reviewAccuracy?: ReviewAccuracy | null;
  firstReviewApprovalRate?: FirstReviewApprovalRate | null;
  freshReviewerEffectiveness?: FreshReviewerEffectiveness | null;
  srCatchRateTargets?: Record<string, number> | null;
}

export function CatchUpReviewQualitySection({
  selfReviewEffectiveness: sre,
  selfReviewPredictionAccuracy: spa,
  selfReviewPredictionAccuracyTrends: spaTrends,
  selfReviewSkipRate: skip,
  reviewAccuracy: ra,
  firstReviewApprovalRate: fra,
  freshReviewerEffectiveness: fre,
  srCatchRateTargets,
}: CatchUpReviewQualitySectionProps) {
  const [expanded, setExpanded] = useState(false);

  const hasSre = sre && (sre.with_self_review_approval_rate != null || sre.without_self_review_approval_rate != null);
  const hasSpa = spa && (spa.prediction_hit_rate != null || spa.false_alarm_rate != null);
  const hasSpaTrends = !!(spaTrends && spaTrends.categories && Object.keys(spaTrends.categories).length > 0);
  const hasRa = ra && (ra.precision != null || ra.recall != null);
  const hasFra = fra && fra.overall != null && fra.sample_size >= 5;
  const hasSkip = !!skip;
  const hasFre = !!(fre && fre.fresh_reviewer_sample_size >= 0);

  if (!hasSre && !hasSpa && !hasSpaTrends && !hasRa && !hasFra && !hasSkip && !hasFre) return null;

  const fraOverall = fra?.overall;
  const fraColor = fraOverall != null
    ? (fraOverall >= 0.8 ? 'text-emerald-400' : fraOverall >= 0.5 ? 'text-amber-400' : 'text-red-400')
    : 'text-neutral-300';
  const pct = (v: number | null | undefined) => v == null ? null : `${Math.round(v * 100)}%`;

  return (
    <div>
      <button
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-[10px] font-bold text-neutral-500 uppercase tracking-widest hover:text-neutral-400 transition-colors"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        Review quality
      </button>
      {expanded && (
        <div className="flex flex-col gap-1 mt-1 ml-1">
          {sre && hasSre && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Self-review</span>
              {sre.with_self_review_approval_rate != null && (
                <span className="text-[10px] text-neutral-400">
                  with <span className="text-emerald-400">{pct(sre.with_self_review_approval_rate)}</span>
                </span>
              )}
              {sre.without_self_review_approval_rate != null && (
                <span className="text-[10px] text-neutral-400">
                  · without <span className="text-neutral-300">{pct(sre.without_self_review_approval_rate)}</span>
                </span>
              )}
              <span className="text-[10px] text-neutral-500">· n={sre.sample_size}</span>
            </div>
          )}
          {spa && hasSpa && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Prediction</span>
              {spa.prediction_hit_rate != null && (
                <span className="text-[10px] text-neutral-400">
                  hit <span className="text-emerald-400">{pct(spa.prediction_hit_rate)}</span>
                </span>
              )}
              {spa.false_alarm_rate != null && (
                <span className="text-[10px] text-neutral-400">
                  · false alarm <span className="text-amber-400">{pct(spa.false_alarm_rate)}</span>
                </span>
              )}
              <span className="text-[10px] text-neutral-500">· n={spa.sample_size}</span>
            </div>
          )}
          {skip && hasSkip && (() => {
            const rate = skip.overall_rate;
            const skipColor = rate <= 0.05 ? 'text-emerald-400' : rate <= 0.15 ? 'text-amber-400' : 'text-red-400';
            const byTypeEntries = Object.entries(skip.by_task_type ?? {});
            return (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">SR Skip</span>
                <span className="text-[10px] text-neutral-400">
                  rate <span className={skipColor}>{pct(rate)}</span>
                </span>
                <span className="text-[10px] text-neutral-500">· {skip.skipped_count}/{skip.sample_size}</span>
                {byTypeEntries.map(([ttype, b]) => (
                  <span key={ttype} className="text-[10px] text-neutral-500">
                    · {ttype} <span className="text-neutral-300">{pct(b.rate)}</span> (n={b.sample_size})
                  </span>
                ))}
              </div>
            );
          })()}
          {hasSpaTrends && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Prediction by category</span>
              <SrTrendBreakdown trends={spaTrends} variant="inline" srCatchRateTargets={srCatchRateTargets} />
            </div>
          )}
          {ra && hasRa && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Review accuracy</span>
              {ra.precision != null && (
                <span className="text-[10px] text-neutral-400">
                  precision <span className="text-emerald-400">{pct(ra.precision)}</span>
                </span>
              )}
              {ra.recall != null && (
                <span className="text-[10px] text-neutral-400">
                  · recall <span className="text-emerald-400">{pct(ra.recall)}</span>
                </span>
              )}
              <span className="text-[10px] text-neutral-500">· n={ra.total_reviews}</span>
            </div>
          )}
          {fra && hasFra && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">First-review approval</span>
              <span className="text-[10px] text-neutral-400">
                <span className={fraColor}>{pct(fra.overall)}</span>
              </span>
              <span className="text-[10px] text-neutral-500">· n={fra.sample_size}</span>
            </div>
          )}
          {fre && hasFre && (() => {
            const n = fre.fresh_reviewer_sample_size;
            const freRate = fre.fresh_reviewer_first_attempt_approval_rate;
            const baseRate = fre.baseline_first_attempt_approval_rate;
            const lift = freRate != null && baseRate != null ? freRate - baseRate : null;
            const freColor = freRate != null
              ? (freRate >= 0.8 ? 'text-emerald-400' : freRate >= 0.5 ? 'text-amber-400' : 'text-red-400')
              : 'text-neutral-300';
            const liftColor = lift == null ? 'text-neutral-400' : lift >= 0.05 ? 'text-emerald-400' : lift >= 0 ? 'text-amber-400' : 'text-red-400';
            const hitEntries = Object.entries(fre.per_category_hit_rate ?? {}).slice(0, 3);
            return (
              <div className="flex flex-col gap-0.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Fresh-reviewer</span>
                  {n < 5 ? (
                    <span className="text-[10px] text-neutral-500">sample too small (n={n})</span>
                  ) : (
                    <>
                      {freRate != null && (
                        <span className="text-[10px] text-neutral-400">approval <span className={freColor}>{pct(freRate)}</span></span>
                      )}
                      {baseRate != null && (
                        <span className="text-[10px] text-neutral-400">· baseline <span className="text-neutral-300">{pct(baseRate)}</span></span>
                      )}
                      {lift != null && (
                        <span className="text-[10px] text-neutral-400">· lift <span className={liftColor}>{lift >= 0 ? '+' : ''}{Math.round(lift * 100)}pp</span></span>
                      )}
                      <span className="text-[10px] text-neutral-500">· n={n}</span>
                    </>
                  )}
                </div>
                {hitEntries.length > 0 && n >= 5 && (
                  <div className="flex flex-wrap items-center gap-1.5 ml-2">
                    <span className="text-[10px] text-neutral-600">hit rate:</span>
                    {hitEntries.map(([cat, rate]) => (
                      <span key={cat} className="text-[10px] text-neutral-500">{cat} <span className="text-neutral-300">{pct(rate)}</span></span>
                    ))}
                  </div>
                )}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}
