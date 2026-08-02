import { useMemo } from 'react';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { type SrTrendWithFixIterations } from './ReviewQualitySection';
import type { SparkPoint, FailureGroup } from './DashboardHome';
import { Skeleton, FAILURE_CLASS_COLORS } from './DashboardMetricCardsPrimitives';
// eslint-disable-next-line react-refresh/only-export-components
export { Skeleton, FAILURE_CLASS_COLORS } from './DashboardMetricCardsPrimitives';

// ---------- metric cards ----------

const SPARK_COLORS: Record<string, string> = { emerald: '#34d399', blue: '#60a5fa', red: '#f87171' };

export function MetricCard({ label, value, color, spark, dataKey, loading }: {
  label: string; value: number; color: string; spark: SparkPoint[]; dataKey: keyof SparkPoint; loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS[color] ?? '#a3a3a3';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-bold font-mono text-${color}-400`}>{value}</p>
      {spark.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
            <AreaChart data={spark}>
              <defs>
                <linearGradient id={`grad-${color}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey={dataKey} stroke={fill} fill={`url(#grad-${color})`} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

const FAILURE_CLASS_SHORT: Record<string, string> = {
  CODE_ERROR: 'code',
  PROVISION_FAILURE: 'provision',
  AGENT_TIMEOUT: 'timeout',
  RATE_LIMIT: 'rate',
  EXTERNAL_API: 'ext api',
  SCOPE_OVERFLOW: 'scope',
};

function windowLabel(windowHours: number): string {
  if (windowHours >= 168) return '7d';
  if (windowHours >= 48) return '48h';
  if (windowHours >= 24) return '24h';
  if (windowHours >= 12) return '12h';
  return '6h';
}

export function FailedCard({ value, groups, spark, loading, windowHours = 24 }: {
  value: number; groups?: FailureGroup[]; spark: SparkPoint[]; loading: boolean; windowHours?: number;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['red'];
  const topGroups = (groups ?? []).slice(0, 3);
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Failed ({windowLabel(windowHours)})</p>
      <p className="text-2xl font-bold font-mono text-red-400">{value}</p>
      {spark.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
            <AreaChart data={spark}>
              <defs>
                <linearGradient id="grad-red" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="failed" stroke={fill} fill="url(#grad-red)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      {value > 0 && topGroups.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {topGroups.map(g => {
            const cls = FAILURE_CLASS_COLORS[g.failure_class] ?? 'bg-neutral-500/20 text-neutral-400';
            const short = FAILURE_CLASS_SHORT[g.failure_class] ?? g.failure_class.toLowerCase();
            return (
              <span key={g.failure_class} className={`text-ui-xs px-1.5 py-0.5 rounded font-medium ${cls}`}>
                {g.count} {short}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function SuccessRateCard({ rate, trend, trendHistory, loading }: {
  rate?: number | null; trend?: string;
  trendHistory?: { success_rate: number | null; completed: number; failed: number }[];
  loading: boolean;
}) {
  const sparkPoints = useMemo(() => (trendHistory ?? []).map(p => ({ v: p.success_rate ?? 0 })), [trendHistory]);
  if (loading) return <Skeleton className="h-20" />;
  const TrendIcon = trend === 'improving' ? TrendingUp : trend === 'declining' ? TrendingDown : Minus;
  const trendColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Success Rate</p>
      <div className="flex items-center gap-2">
        <p className="text-2xl font-bold font-mono text-neutral-100">{rate != null ? `${Math.round(rate * 100)}%` : '--'}</p>
        <TrendIcon className={`w-4 h-4 ${trendColor}`} />
      </div>
      {sparkPoints.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
            <AreaChart data={sparkPoints}>
              <defs>
                <linearGradient id="grad-success" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#e5e5e5" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#e5e5e5" stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke="#e5e5e5" fill="url(#grad-success)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

export function ConversionRateCard({ rate, loading }: {
  rate?: number | null;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const pct = rate != null ? Math.round(rate * 100) : null;
  const color = pct == null ? 'text-neutral-500' : pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Conversion Rate</p>
      <p className={`text-2xl font-bold font-mono ${color}`}>{pct != null ? `${pct}%` : '--'}</p>
      <p className="text-ui-sm text-neutral-600 mt-1">PRs merged / PR-eligible tasks created</p>
    </div>
  );
}

export function AcceptanceRateCard({ rate, loading }: {
  rate?: number | null;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const pct = rate != null ? Math.round(rate * 100) : null;
  const color = pct == null ? 'text-neutral-500' : pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Acceptance Rate</p>
      <p className={`text-2xl font-bold font-mono ${color}`}>{pct != null ? `${pct}%` : '--'}</p>
      <p className="text-ui-sm text-neutral-600 mt-1">PRs merged / (merged + rejected)</p>
    </div>
  );
}

export function CostCard({ totalCost, trendHistory, loading, windowHours = 24 }: {
  totalCost?: number | null;
  trendHistory?: { period_cost_usd?: number | null }[];
  loading: boolean;
  windowHours?: number;
}) {
  const sparkPoints = useMemo(() => (trendHistory ?? []).map(p => ({ v: p.period_cost_usd ?? 0 })), [trendHistory]);
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['blue'];
  const display = totalCost == null ? '--' : totalCost < 10 ? `$${totalCost.toFixed(2)}` : `$${Math.round(totalCost)}`;
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Cost ({windowLabel(windowHours)})</p>
      <p className="text-2xl font-bold font-mono text-blue-400">{display}</p>
      {sparkPoints.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
            <AreaChart data={sparkPoints}>
              <defs>
                <linearGradient id="grad-cost" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke={fill} fill="url(#grad-cost)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

const MIN_SR_CATCH_FINDINGS = 5;

// eslint-disable-next-line react-refresh/only-export-components
export function deriveReviewerFindingCount(trends: SrTrendWithFixIterations | null | undefined): number {
  const categories = trends?.categories;
  if (!categories) return 0;
  return Object.values(categories).reduce(
    (sum, cat) => sum + (cat.current_reviewer_finding_count ?? cat.current_finding_count ?? cat.current_human_count ?? 0),
    0,
  );
}

export function SrCatchRateCard({ rate, trends, loading, onScrollToReviewQuality }: {
  rate?: number | null;
  trends?: SrTrendWithFixIterations | null;
  loading: boolean;
  onScrollToReviewQuality?: () => void;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const reviewerFindingCount = deriveReviewerFindingCount(trends);
  if (rate == null || reviewerFindingCount < MIN_SR_CATCH_FINDINGS) {
    return (
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4 cursor-pointer hover:border-neutral-700/80 transition-colors" onClick={onScrollToReviewQuality}>
        <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">SR Catch Rate</p>
        <p className="text-2xl font-bold font-mono text-neutral-500">too small</p>
        <p className="text-ui-sm text-neutral-600 mt-1">n={reviewerFindingCount} findings</p>
      </div>
    );
  }

  const overallPrevRate = trends?.previous_overall_hit_rate ?? null;
  const delta = overallPrevRate != null ? rate - overallPrevRate : null;
  const verdict = trends?.overall_trend_verdict ?? null;
  const significant = trends?.overall_trend_significant ?? null;

  const pct = Math.round(rate * 100);
  const color = rate >= 0.7 ? 'text-emerald-400' : rate >= 0.4 ? 'text-amber-400' : 'text-red-400';
  const deltaPp = delta != null ? Math.round(delta * 100) : 0;
  // Use backend significance-gated verdict when available; fall back to neutral when significance unknown
  const improving = verdict === 'improving' || (verdict == null && significant !== false && delta != null && delta > 0.01);
  const declining = verdict === 'declining' || (verdict == null && significant !== false && delta != null && delta < -0.01);
  const deltaColor = improving ? 'text-emerald-500' : declining ? 'text-red-500' : 'text-neutral-500';
  const deltaIcon = improving ? '↑' : declining ? '↓' : '→';
  // De-emphasize delta when backend reports non-significant change
  const deltaOpacity = (verdict === 'stable' || significant === false) ? 'opacity-50' : '';

  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4 cursor-pointer hover:border-neutral-700/80 transition-colors" onClick={onScrollToReviewQuality}>
      <div className="flex items-center justify-between mb-1">
        <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">SR Catch Rate</p>
        {trends?.is_stale && (
          <span className="text-ui-xs text-amber-400 font-mono" title={`Trend data is stale — last computed ${trends.computed_at ? new Date(trends.computed_at).toLocaleDateString() : 'unknown'}`}>stale</span>
        )}
        {trends?.data_sufficient === false && !trends?.is_stale && (
          <span className="text-ui-xs text-amber-500 font-mono" title="Insufficient reviewer-finding data — displayed numbers are frozen">frozen</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <p className={`text-2xl font-bold font-mono ${color}`}>{pct}%</p>
        {delta != null && (
          <span className={`text-sm font-semibold ${deltaColor} ${deltaOpacity}`}>
            {deltaIcon} {deltaPp >= 0 ? '+' : ''}{deltaPp}pp
          </span>
        )}
      </div>
      <p className="text-ui-sm text-neutral-600 mt-1">n={reviewerFindingCount} findings</p>
    </div>
  );
}
