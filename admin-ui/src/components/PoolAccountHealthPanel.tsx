import { useState } from 'react';
import { snapshotFreshness } from '../utils/time';

// ---------- types ----------

type AccountHealthState = 'healthy' | 'probing' | 'disqualified' | 'disabled';

interface RegressionStats {
  account_rate: number;
  project_median: number;
  sample_size: number;
  distinct_accounts: number;
  started_at?: string;
}

interface RateLimitedModel {
  model: string;
  cooldown_seconds_remaining: number;
  cooldown_type: 'rate_limit' | 'quota_exhausted' | 'regression';
  regression_stats?: RegressionStats;
}

// Mirrors backend max_plan_pool._usage_snapshot_summary (additive, may be absent).
// Utilizations are percentages in [0, 100] or null; resets_at keys are
// "five_hour" / "seven_day" mapping to ISO-8601 reset timestamps (or null).
export interface UsageSnapshot {
  known: boolean;
  dark_reason?: string | null;
  five_hour_utilization: number | null;
  seven_day_utilization: number | null;
  resets_at: Record<string, string | null>;
  probed_at: string | null;
  stale: boolean;
  /** Additive diagnostic receipt. A failed attempt never removes last-good usage. */
  last_attempt_at?: string | null;
  last_attempt_status?: string | null;
  last_attempt_failure_kind?: string | null;
}

export interface AccountHealthEntry {
  account_id: string;
  label: string;
  provider: string;
  state: AccountHealthState;
  disabled_reason?: string | null;
  health_score: number;
  success_rate_7d?: number | null;
  completions_24h: number;
  rl_events_24h: number;
  rate_limited_models: RateLimitedModel[];
  failure_class_counts: Partial<Record<string, number>>;
  failure_class_buckets?: { provider?: number; code?: number; infrastructure?: number };
  usage_snapshot?: UsageSnapshot;
}

// ---------- constants ----------

const ACCOUNT_STATE_BADGES: Record<AccountHealthState, string> = {
  healthy: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  probing: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  disqualified: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  disabled: 'bg-red-500/20 text-red-400 border-red-500/30',
};

const FAILURE_CLASS_BAR_COLORS: Record<string, string> = {
  CODE_ERROR: 'bg-red-400/70',
  RATE_LIMIT: 'bg-yellow-400/70',
  EXTERNAL_API: 'bg-purple-400/70',
  PROVISION_FAILURE: 'bg-orange-400/70',
  MIGRATION_ERROR: 'bg-pink-400/70',
  AGENT_TIMEOUT: 'bg-amber-400/70',
  SCOPE_OVERFLOW: 'bg-teal-400/70',
};

const FAILURE_CLASS_SHORT: Record<string, string> = {
  CODE_ERROR: 'code',
  PROVISION_FAILURE: 'provision',
  AGENT_TIMEOUT: 'timeout',
  RATE_LIMIT: 'rate',
  EXTERNAL_API: 'ext api',
  SCOPE_OVERFLOW: 'scope',
};

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-neutral-800/60 rounded ${className}`} />;
}

// Utilization is "fraction of the window's budget consumed" — higher is worse
// (closer to exhaustion). null util keeps neutral styling.
function usageColor(util: number | null): string {
  if (util == null) return 'text-neutral-400';
  if (util >= 90) return 'text-red-400';
  if (util >= 70) return 'text-amber-400';
  return 'text-emerald-400';
}

function formatResetAt(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatTimeOnly(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
}

function usageProbeLabel(snapshot: UsageSnapshot): string {
  return snapshot.last_attempt_failure_kind ?? snapshot.dark_reason ?? 'unknown';
}

function maxUsage(snapshot: UsageSnapshot | undefined): number {
  if (!snapshot?.known) return 0;
  return Math.max(snapshot.five_hour_utilization ?? 0, snapshot.seven_day_utilization ?? 0);
}

function accountNextAction(acct: AccountHealthEntry, totalFailures: number): { label: string; cls: string } {
  const usage = maxUsage(acct.usage_snapshot);
  const quotaCooldown = acct.rate_limited_models.some(model => model.cooldown_type === 'quota_exhausted');
  const regressionCooldown = acct.rate_limited_models.some(model => model.cooldown_type === 'regression');
  if (acct.state === 'disabled') {
    return { label: acct.disabled_reason ? `Disabled: ${acct.disabled_reason}` : 'Disabled: enable or rotate credentials', cls: 'text-red-300' };
  }
  if (quotaCooldown) return { label: 'Hold traffic: daily quota exhausted', cls: 'text-red-300' };
  if (regressionCooldown) return { label: 'Route around regression cooldown', cls: 'text-amber-300' };
  if (acct.state === 'disqualified') return { label: 'Disqualified: wait for probe or inspect failures', cls: 'text-amber-300' };
  if (usage >= 90) return { label: 'Near quota: move traffic or wait for reset', cls: 'text-red-300' };
  if (usage >= 70) return { label: 'Usage pressure: keep as backup', cls: 'text-amber-300' };
  if (acct.rate_limited_models.length > 0) return { label: 'Model cooldown active: route around it', cls: 'text-amber-300' };
  if (totalFailures > 0 || acct.rl_events_24h > 0) return { label: 'Degraded: watch recent failures', cls: 'text-amber-300' };
  if (acct.state === 'probing') return { label: 'Probing: wait for health sample', cls: 'text-blue-300' };
  return { label: 'Ready for dispatch', cls: 'text-emerald-300' };
}

// ---------- component ----------

// Response is considered visibly stale once the cache fallback is older than
// this many seconds (3 polls of the 30s dashboard refresh). Distinct from the
// per-account usage_snapshot.stale flag, which is a provider-side probe signal.
const RESPONSE_STALE_AFTER_SECONDS = 90;

export function PoolAccountHealthPanel({ accounts, loading, servedStale = false, generatedAt = null }: {
  accounts: AccountHealthEntry[] | null;
  loading: boolean;
  // Response-level cache-fallback freshness (additive; covers the WHOLE
  // response, not one account's provider snapshot). When the backend served a
  // last-good payload on compute failure, servedStale is true and generatedAt
  // is the payload's original timestamp.
  servedStale?: boolean;
  generatedAt?: string | null;
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  if (loading) return <Skeleton className="h-16 w-full" />;
  if (!accounts || accounts.length === 0) return null;

  const freshness = snapshotFreshness(generatedAt, null, RESPONSE_STALE_AFTER_SECONDS);
  const showStale = servedStale;
  const stateCounts = accounts.reduce<Record<AccountHealthState, number>>(
    (acc, account) => {
      acc[account.state] += 1;
      return acc;
    },
    { healthy: 0, probing: 0, disqualified: 0, disabled: 0 },
  );
  const usageKnown = accounts.filter(account => account.usage_snapshot?.known).length;
  const usagePressure = accounts.filter(account => maxUsage(account.usage_snapshot) >= 70).length;
  const cooldownModels = accounts.reduce((sum, account) => sum + account.rate_limited_models.length, 0);
  const regressionModels = accounts.reduce(
    (sum, account) => sum + account.rate_limited_models.filter(model => model.cooldown_type === 'regression').length,
    0,
  );

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Provider Account Health</h2>
        {showStale && (
          <span
            className="text-ui-xs font-semibold px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/15 text-amber-300"
            title={`This whole panel is a cached fallback — the backend could not recompute it on the last poll${freshness.asOfLabel ? `; data is from ${freshness.asOfLabel}` : ''}`}
          >
            stale{freshness.asOfLabel ? ` · as of ${freshness.asOfLabel}` : ''}
          </span>
        )}
      </div>
      <div
        data-testid="account-health-summary"
        className="mb-2 grid grid-cols-2 overflow-hidden rounded-md border border-neutral-800/60 bg-neutral-950/30 sm:grid-cols-4"
      >
        <div className="border-r border-b border-neutral-800/60 px-2.5 py-2 sm:border-b-0">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Healthy</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-emerald-300">
            {stateCounts.healthy}/{accounts.length}
          </div>
        </div>
        <div className="border-r-0 border-b border-neutral-800/60 px-2.5 py-2 sm:border-r sm:border-b-0">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Probing</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-blue-300">{stateCounts.probing}</div>
        </div>
        <div className="border-r border-neutral-800/60 px-2.5 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Suppressed</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-amber-300">
            {stateCounts.disqualified + stateCounts.disabled}
          </div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{cooldownModels} model cooldowns</div>
        </div>
        <div className="px-2.5 py-2">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Usage</div>
          <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">
            {usagePressure}/{usageKnown}
          </div>
          <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{regressionModels} regressions</div>
        </div>
      </div>
      <div className="space-y-1">
        {accounts.map(acct => {
          const expanded = expandedId === acct.account_id;
          const stateBadge = ACCOUNT_STATE_BADGES[acct.state] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
          const scoreColor = acct.health_score >= 0.7 ? 'text-emerald-400' : acct.health_score >= 0.4 ? 'text-amber-400' : 'text-red-400';
          const counts = Object.entries(acct.failure_class_counts).filter(([, v]) => (v ?? 0) > 0);
          const totalFailures = counts.reduce((s, [, v]) => s + (v ?? 0), 0);
          const dominantEntry = counts.length > 0 ? counts.reduce((a, b) => ((b[1] ?? 0) > (a[1] ?? 0) ? b : a)) : null;
          const sr7d = acct.success_rate_7d;
          const sr7dColor = sr7d == null ? '' : sr7d >= 0.7 ? 'text-emerald-400' : sr7d >= 0.4 ? 'text-amber-400' : 'text-red-400';
          const dimmed = acct.state === 'disabled' || acct.state === 'disqualified';
          const action = accountNextAction(acct, totalFailures);
          return (
            <div key={acct.account_id} className={`bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-2.5${dimmed ? ' opacity-50' : ''}`}>
              <button
                type="button"
                className="min-h-11 w-full text-left"
                onClick={() => setExpandedId(expanded ? null : acct.account_id)}
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`shrink-0 text-ui-xs font-semibold px-1.5 py-0.5 rounded border uppercase ${stateBadge}`}>
                    {acct.state}
                  </span>
                  <span className="text-xs text-neutral-200 font-medium">{acct.label}</span>
                  <span className="text-ui-sm text-neutral-500">{acct.provider}</span>
                  <span className={`text-ui-sm font-mono font-bold ${scoreColor}`} title="Health score">
                    {Math.round(acct.health_score * 100)}%
                  </span>
                  {sr7d != null && (
                    <span className={`text-ui-sm font-mono ${sr7dColor}`} title="7-day success rate">
                      {Math.round(sr7d * 100)}% 7d
                    </span>
                  )}
                  <span className="text-ui-sm text-neutral-400" title="24h completions · rate-limit events">
                    {acct.completions_24h}c {acct.rl_events_24h}rl
                  </span>
                  {acct.usage_snapshot && (
                    acct.usage_snapshot.known ? (
                      <span
                        className={`flex items-center gap-1 text-ui-sm font-mono${acct.usage_snapshot.stale ? ' opacity-60' : ''}`}
                        title={`Usage headroom — 5h / 7d window utilization${acct.usage_snapshot.stale ? ' (stale)' : ''}`}
                      >
                        <span className={usageColor(acct.usage_snapshot.five_hour_utilization)}>
                          5h {acct.usage_snapshot.five_hour_utilization == null ? '?' : `${Math.round(acct.usage_snapshot.five_hour_utilization)}%`}
                        </span>
                        <span className={usageColor(acct.usage_snapshot.seven_day_utilization)}>
                          7d {acct.usage_snapshot.seven_day_utilization == null ? '?' : `${Math.round(acct.usage_snapshot.seven_day_utilization)}%`}
                        </span>
                        {acct.usage_snapshot.stale && (
                          <span className="text-neutral-400" title={`Snapshot is stale${acct.usage_snapshot.probed_at ? ` (probed at ${acct.usage_snapshot.probed_at})` : ''}`}>
                            stale{acct.usage_snapshot.probed_at ? ` @${formatTimeOnly(acct.usage_snapshot.probed_at)}` : ''}
                          </span>
                        )}
                        {acct.usage_snapshot.last_attempt_failure_kind && (
                          <span
                            className="text-amber-400"
                            title={`Latest usage probe failed: ${acct.usage_snapshot.last_attempt_failure_kind}${acct.usage_snapshot.last_attempt_at ? ` at ${acct.usage_snapshot.last_attempt_at}` : ''}. Showing the last known reading.`}
                          >
                            probe {acct.usage_snapshot.last_attempt_failure_kind}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span
                        className={`text-ui-sm font-mono ${acct.usage_snapshot.dark_reason === 'unsupported' ? 'text-neutral-500' : 'text-amber-400'}`}
                        title={`Usage probe: ${usageProbeLabel(acct.usage_snapshot)}${acct.usage_snapshot.last_attempt_at ? ` (last attempted ${acct.usage_snapshot.last_attempt_at})` : ''}`}
                      >
                        usage {usageProbeLabel(acct.usage_snapshot)}
                      </span>
                    )
                  )}
                  {totalFailures > 0 && (
                    <div className="flex flex-col items-end gap-0.5 ml-auto">
                      {acct.failure_class_buckets && (
                        <div className="flex items-center gap-1.5 text-ui-xs">
                          <span className="text-yellow-400/80" title="provider: rate-limit + external API">
                            <span className="inline-block w-1.5 h-1.5 rounded-sm bg-yellow-400/70 align-middle mr-0.5" />
                            {acct.failure_class_buckets.provider ?? 0}p
                          </span>
                          <span className="text-red-400/80" title="code: code error + migration">
                            <span className="inline-block w-1.5 h-1.5 rounded-sm bg-red-400/70 align-middle mr-0.5" />
                            {acct.failure_class_buckets.code ?? 0}c
                          </span>
                          <span className="text-orange-400/80" title="infra: provision + timeout + scope">
                            <span className="inline-block w-1.5 h-1.5 rounded-sm bg-orange-400/70 align-middle mr-0.5" />
                            {acct.failure_class_buckets.infrastructure ?? 0}i
                          </span>
                        </div>
                      )}
                      <div className="flex items-center gap-1">
                        {dominantEntry && (
                          <span className="text-ui-xs text-neutral-400" title="Dominant failure class (24h)">
                            {FAILURE_CLASS_SHORT[dominantEntry[0]] ?? dominantEntry[0].toLowerCase()}:{dominantEntry[1]}
                          </span>
                        )}
                        <div className="flex items-center gap-0.5">
                          {counts.sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0)).map(([cls, count]) => (
                            <div
                              key={cls}
                              title={`${FAILURE_CLASS_SHORT[cls] ?? cls}: ${count}`}
                              className={`h-2 rounded-sm ${FAILURE_CLASS_BAR_COLORS[cls] ?? 'bg-neutral-500'}`}
                              style={{ width: `${Math.max(4, Math.round(((count ?? 0) / totalFailures) * 32))}px` }}
                            />
                          ))}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
                {acct.disabled_reason && (
                  <p className="text-ui-sm text-red-400/80 mt-0.5 truncate">{acct.disabled_reason}</p>
                )}
                {acct.rate_limited_models.filter(m => m.cooldown_type === 'regression' && m.regression_stats).map(m => {
                  const rs = m.regression_stats;
                  if (!rs) return null;
                  const modelLabel = m.model.includes('/') ? (m.model.split('/').pop() ?? m.model) : m.model;
                  return (
                    <p key={m.model} className="text-ui-sm text-amber-400/80 mt-0.5">
                      regression on {modelLabel}: {Math.round(rs.account_rate * 100)}% vs project median {Math.round(rs.project_median * 100)}% (n={rs.sample_size} over {rs.distinct_accounts} accounts)
                    </p>
                  );
                })}
                <p className={`mt-0.5 text-ui-sm ${action.cls}`} data-testid="account-health-next-action">
                  Next: {action.label}
                </p>
              </button>
              {expanded && acct.usage_snapshot && (
                <div className="mt-2 pt-2 border-t border-neutral-800/50 text-ui-sm text-neutral-400 space-y-0.5" title="Usage headroom detail">
                  {acct.usage_snapshot.known ? (
                    <>
                      <div className="flex items-center gap-3">
                        <span className={`font-mono ${usageColor(acct.usage_snapshot.five_hour_utilization)}`}>
                          5h {acct.usage_snapshot.five_hour_utilization == null ? '?' : `${Math.round(acct.usage_snapshot.five_hour_utilization)}%`}
                        </span>
                        <span className="text-neutral-500">resets {formatResetAt(acct.usage_snapshot.resets_at.five_hour ?? null)}</span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`font-mono ${usageColor(acct.usage_snapshot.seven_day_utilization)}`}>
                          7d {acct.usage_snapshot.seven_day_utilization == null ? '?' : `${Math.round(acct.usage_snapshot.seven_day_utilization)}%`}
                        </span>
                        <span className="text-neutral-500">resets {formatResetAt(acct.usage_snapshot.resets_at.seven_day ?? null)}</span>
                      </div>
                      <div className="text-neutral-500">
                        probed {formatResetAt(acct.usage_snapshot.probed_at)}{acct.usage_snapshot.stale ? ' · stale' : ''}
                      </div>
                    </>
                  ) : (
                    <div className="text-neutral-500">
                      usage headroom {acct.usage_snapshot.dark_reason === 'unsupported' ? 'unsupported' : 'unavailable'} — {usageProbeLabel(acct.usage_snapshot)}
                      {acct.usage_snapshot.last_attempt_at ? ` (last attempted ${formatResetAt(acct.usage_snapshot.last_attempt_at)})` : acct.usage_snapshot.probed_at ? ` (last probed ${formatResetAt(acct.usage_snapshot.probed_at)})` : ''}
                    </div>
                  )}
                  {acct.usage_snapshot.known && acct.usage_snapshot.last_attempt_failure_kind && (
                    <div className="text-amber-400">
                      latest probe {acct.usage_snapshot.last_attempt_failure_kind}
                      {acct.usage_snapshot.last_attempt_at ? ` at ${formatResetAt(acct.usage_snapshot.last_attempt_at)}` : ''}; showing last known reading
                    </div>
                  )}
                </div>
              )}
              {expanded && acct.rate_limited_models.length > 0 && (
                <div className="mt-2 pt-2 border-t border-neutral-800/50 flex flex-wrap gap-1">
                  {acct.rate_limited_models.map(m => {
                    const isQuota = m.cooldown_type === 'quota_exhausted';
                    const badge = isQuota
                      ? 'bg-red-500/20 text-red-400 border-red-500/30'
                      : 'bg-amber-500/20 text-amber-400 border-amber-500/30';
                    const modelLabel = m.model.includes('/') ? (m.model.split('/').pop() ?? m.model) : m.model;
                    const durLabel = isQuota ? '24h quota' : `${Math.ceil(m.cooldown_seconds_remaining / 60)}m`;
                    return (
                      <span key={m.model} className={`text-ui-xs font-mono px-1.5 py-0.5 rounded border ${badge}`}>
                        {modelLabel} · {durLabel}
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
