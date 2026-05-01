import { useState } from 'react';

// ---------- types ----------

type AccountHealthState = 'healthy' | 'probing' | 'disqualified' | 'disabled';

interface RateLimitedModel {
  model: string;
  cooldown_seconds_remaining: number;
  cooldown_type: 'rate_limit' | 'quota_exhausted';
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

// ---------- component ----------

export function PoolAccountHealthPanel({ accounts, loading }: {
  accounts: AccountHealthEntry[] | null;
  loading: boolean;
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  if (loading) return <Skeleton className="h-16 w-full" />;
  if (!accounts || accounts.length === 0) return null;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Provider Account Health</h2>
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
          return (
            <div key={acct.account_id} className={`bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-2.5${dimmed ? ' opacity-50' : ''}`}>
              <button
                type="button"
                className="w-full text-left"
                onClick={() => setExpandedId(expanded ? null : acct.account_id)}
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase ${stateBadge}`}>
                    {acct.state}
                  </span>
                  <span className="text-xs text-neutral-200 font-medium">{acct.label}</span>
                  <span className="text-[10px] text-neutral-500">{acct.provider}</span>
                  <span className={`text-[10px] font-mono font-bold ${scoreColor}`} title="Health score">
                    {Math.round(acct.health_score * 100)}%
                  </span>
                  {sr7d != null && (
                    <span className={`text-[10px] font-mono ${sr7dColor}`} title="7-day success rate">
                      {Math.round(sr7d * 100)}% 7d
                    </span>
                  )}
                  <span className="text-[10px] text-neutral-400" title="24h completions · rate-limit events">
                    {acct.completions_24h}c {acct.rl_events_24h}rl
                  </span>
                  {totalFailures > 0 && (
                    <div className="flex flex-col items-end gap-0.5 ml-auto">
                      {acct.failure_class_buckets && (
                        <div className="flex items-center gap-1.5 text-[9px]">
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
                          <span className="text-[9px] text-neutral-400" title="Dominant failure class (24h)">
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
                  <p className="text-[10px] text-red-400/80 mt-0.5 truncate">{acct.disabled_reason}</p>
                )}
              </button>
              {expanded && acct.rate_limited_models.length > 0 && (
                <div className="mt-2 pt-2 border-t border-neutral-800/50 flex flex-wrap gap-1">
                  {acct.rate_limited_models.map(m => {
                    const isQuota = m.cooldown_type === 'quota_exhausted';
                    const badge = isQuota
                      ? 'bg-red-500/20 text-red-400 border-red-500/30'
                      : 'bg-amber-500/20 text-amber-400 border-amber-500/30';
                    const modelLabel = m.model.includes('/') ? m.model.split('/').pop()! : m.model;
                    const durLabel = isQuota ? '24h quota' : `${Math.ceil(m.cooldown_seconds_remaining / 60)}m`;
                    return (
                      <span key={m.model} className={`text-[9px] font-mono px-1.5 py-0.5 rounded border ${badge}`}>
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
