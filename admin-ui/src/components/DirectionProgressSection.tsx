import { useState, useEffect, useMemo } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { timeAgo } from '../utils/time';
import { TAXONOMY_ORDER, TAXONOMY_LABELS, TAXONOMY_COLORS, STAGE_ORDER, STAGE_COLORS } from './taxonomyConstants';

export interface DirectionProgress {
  direction_id: string;
  direction_text: string;
  priority: string;
  cycles_in_window: number;
  tasks_created: number;
  prs_merged: number;
  possibly_saturated: boolean;
  possibly_complete?: boolean;
  completion_criteria?: string | null;
  completion_suggestion?: string | null;
  latest_insights?: string[];
  recent_finding_titles?: string[];
  recent_failure_titles?: string[];
  latest_research_at?: string | null;
  prs_rejected?: number;
  acceptance_rate?: number | null;
  first_attempt_success_rate?: number | null;
  quality_score?: number | null;
  cost_per_merged_pr?: number | null;
  cost_per_merge_exceeds_threshold?: boolean | null;
  estimated_cost_usd?: number | null;
  cost_by_scope?: Record<string, number> | null;
  cost_by_type?: Record<string, number> | null;
  cost_by_status?: Record<string, number> | null;
  cost_by_provider_model?: Record<string, number> | null;
  avg_time_to_merge_hours?: number | null;
  auto_paused?: boolean;
  paused_reason?: string | null;
  needs_manual_intervention?: boolean;
  scope_breakdown?: Record<string, {
    created?: number;
    merged?: number;
    merged_cross_module?: number;
    failed?: number;
    failed_infra?: number;
    failed_code_error?: number;
    failed_scope_overflow?: number;
    calibrated_in?: number;
    calibrated_out?: number;
    failure_stages?: Record<string, number>;
    avg_expansion_ratio_merged?: number;
    avg_expansion_ratio_failed?: number;
  }> | null;
  type_breakdown?: Record<string, { created?: number; merged?: number; failed?: number }> | null;
  taxonomy_summary?: Record<string, number> | null;
  stage_dropout?: Record<string, number> | null;
  rejection_reasons?: { task_id?: string; title?: string; rejection_note?: string }[] | null;
  lifetime_cycles?: number;
  lifetime_prs_merged?: number;
  lifetime_prs_rejected?: number;
  lifetime_tasks_failed?: number;
  lifetime_conversion_rate?: number | null;
  review_quality?: {
    reviews_dismissed?: number;
    reviews_overridden?: number;
    user_issues_caught?: number;
  } | null;
  filter_funnel?: {
    findings_produced?: number;
    hash_dedup?: number;
    word_overlap?: number;
    file_overlap?: number;
    type_filtered?: number;
    empty_prompt?: number;
    batch_cap?: number;
    tasks_created?: number;
  } | null;
  prompt_quality_hint?: string | null;
  decomposition_summary?: {
    plans_assessed?: number;
    avg_subtask_merge_rate?: number | null;
    avg_subtask_count?: number;
    avg_cross_module_count?: number;
  } | null;
  cost_budget_usd?: number | null;
  budget_utilization_pct?: number | null;
  budget_exceeded?: boolean;
}

type DirectionSortKey = 'default' | 'acceptance_asc' | 'cost_desc' | 'first_attempt_asc';

const DIRECTION_SORT_STORAGE_KEY = 'catchup.directionProgress.sort';

export function DirectionProgressSection({ directions, medianCostPerMergedPr }: { directions: DirectionProgress[]; medianCostPerMergedPr?: number | null }) {
  const [expandedDirections, setExpandedDirections] = useState<Set<string>>(new Set());
  const [sortKey, setSortKey] = useState<DirectionSortKey>(() => {
    if (typeof window === 'undefined') return 'default';
    const stored = window.localStorage.getItem(DIRECTION_SORT_STORAGE_KEY);
    if (stored === 'acceptance_asc' || stored === 'cost_desc' || stored === 'first_attempt_asc' || stored === 'default') {
      return stored;
    }
    return 'default';
  });

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(DIRECTION_SORT_STORAGE_KEY, sortKey);
  }, [sortKey]);

  const toggleDirection = (id: string) => {
    setExpandedDirections(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const sortedDirections = useMemo(() => {
    if (sortKey === 'default') return directions;
    const arr = [...directions];
    const numOr = (v: number | null | undefined, fallback: number) => (typeof v === 'number' ? v : fallback);
    if (sortKey === 'acceptance_asc') {
      arr.sort((a, b) => numOr(a.acceptance_rate, Infinity) - numOr(b.acceptance_rate, Infinity));
    } else if (sortKey === 'cost_desc') {
      arr.sort((a, b) => numOr(b.estimated_cost_usd, -Infinity) - numOr(a.estimated_cost_usd, -Infinity));
    } else if (sortKey === 'first_attempt_asc') {
      arr.sort((a, b) => numOr(a.first_attempt_success_rate, Infinity) - numOr(b.first_attempt_success_rate, Infinity));
    }
    return arr;
  }, [directions, sortKey]);

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between mb-0.5">
        <div className="text-[11px] font-bold text-neutral-500 uppercase tracking-widest">Directions</div>
        <select
          value={sortKey}
          onChange={e => setSortKey(e.target.value as DirectionSortKey)}
          className="text-[10px] bg-neutral-950 border border-neutral-800/80 text-neutral-400 rounded px-1.5 py-0.5 cursor-pointer hover:bg-neutral-800/30"
          aria-label="Sort directions"
          title="Sort directions"
        >
          <option value="default">Default order</option>
          <option value="acceptance_asc">Acceptance rate ↑</option>
          <option value="cost_desc">Cost ↓</option>
          <option value="first_attempt_asc">First-attempt ↑</option>
        </select>
      </div>
      {sortedDirections.map(dp => {
        const isExpanded = expandedDirections.has(dp.direction_id);
        return (
          <div key={dp.direction_id}>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/30 w-full text-left min-w-0"
              onClick={() => toggleDirection(dp.direction_id)}
              aria-expanded={isExpanded}
            >
              {isExpanded ? <ChevronUp className="w-3 h-3 shrink-0 text-neutral-600" /> : <ChevronDown className="w-3 h-3 shrink-0 text-neutral-600" />}
              <span className="flex-1 min-w-0 truncate text-neutral-400">
                {(dp.direction_text || dp.direction_id).slice(0, 50)}
              </span>
              <div className="flex flex-wrap items-center gap-1 min-w-0">
                {dp.cycles_in_window > 0 && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-500 border border-neutral-700/30 px-1.5 py-0.5 rounded">
                    {dp.cycles_in_window} cycle{dp.cycles_in_window !== 1 ? 's' : ''}
                  </span>
                )}
                {dp.tasks_created > 0 && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-500 border border-neutral-700/30 px-1.5 py-0.5 rounded">
                    {dp.tasks_created} task{dp.tasks_created !== 1 ? 's' : ''}
                  </span>
                )}
                {dp.prs_merged > 0 && (
                  <span className="text-[9px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 px-1.5 py-0.5 rounded">
                    {dp.prs_merged} PR{dp.prs_merged !== 1 ? 's' : ''}
                  </span>
                )}
                {typeof dp.prs_rejected === 'number' && dp.prs_rejected > 0 && (
                  <span className="text-[9px] bg-red-500/10 text-red-400 border border-red-500/20 px-1.5 py-0.5 rounded" title="PRs rejected">
                    {dp.prs_rejected} rejected
                  </span>
                )}
                {dp.possibly_saturated && (
                  <span className="text-[9px] bg-amber-500/10 text-amber-500 border border-amber-500/20 px-1.5 py-0.5 rounded">
                    saturated
                  </span>
                )}
                {dp.auto_paused && (
                  <span className="text-[9px] bg-red-500/20 text-red-400 border border-red-500/30 px-1.5 py-0.5 rounded" title={dp.paused_reason || "Direction auto-paused"}>
                    paused
                  </span>
                )}
                {dp.needs_manual_intervention && (
                  <span className="text-[9px] bg-red-500/20 text-red-400 border border-red-500/30 px-1.5 py-0.5 rounded" title="Needs manual intervention: repeated failures or remediation cap hit">
                    needs intervention
                  </span>
                )}
                {dp.possibly_complete && (
                  <span className="text-[9px] bg-sky-500/10 text-sky-400 border border-sky-500/20 px-1.5 py-0.5 rounded" title={dp.completion_suggestion || "Direction may be complete"}>
                    possibly complete
                  </span>
                )}
                {dp.priority && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded" title="Direction priority">
                    {dp.priority}
                  </span>
                )}
                {typeof dp.acceptance_rate === 'number' && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded" title="Operator acceptance rate">
                    {Math.round(dp.acceptance_rate * 100)}% acc
                  </span>
                )}
                {typeof dp.first_attempt_success_rate === 'number' && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded" title="First-attempt success rate">
                    {Math.round(dp.first_attempt_success_rate * 100)}% 1st
                  </span>
                )}
                {typeof dp.quality_score === 'number' && (
                  <span
                    className={`text-[9px] px-1.5 py-0.5 rounded border ${
                      dp.quality_score >= 0.6
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                        : dp.quality_score >= 0.3
                        ? 'bg-amber-500/10 text-amber-500 border-amber-500/20'
                        : 'bg-red-500/10 text-red-400 border-red-500/20'
                    }`}
                    title="Direction quality score (taxonomy health, wasted cycles, scope calibration)"
                  >
                    {Math.round(dp.quality_score * 100)}% q
                  </span>
                )}
                {typeof dp.cost_per_merged_pr === 'number' && dp.cost_per_merged_pr > 0 && (
                  <span
                    className={`text-[9px] px-1.5 py-0.5 rounded font-mono border ${dp.cost_per_merge_exceeds_threshold ? 'bg-red-900/30 text-red-400 border-red-500/30' : 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30'}`}
                    title={dp.cost_per_merge_exceeds_threshold
                      ? `Cost per merged PR exceeds threshold${medianCostPerMergedPr != null ? ` (project median: $${medianCostPerMergedPr.toFixed(2)})` : ''}`
                      : 'Average cost per merged PR'}
                  >
                    {dp.cost_per_merge_exceeds_threshold && '⚠ '}${dp.cost_per_merged_pr.toFixed(2)}/PR
                  </span>
                )}
                {typeof dp.avg_time_to_merge_hours === 'number' && dp.avg_time_to_merge_hours > 0 && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded font-mono" title="Average hours from PR creation to merge">
                    {dp.avg_time_to_merge_hours < 1
                      ? `${Math.round(dp.avg_time_to_merge_hours * 60)}m/merge`
                      : `${dp.avg_time_to_merge_hours.toFixed(1)}h/merge`}
                  </span>
                )}
                {(() => {
                  const spend = dp.estimated_cost_usd;
                  const budget = dp.cost_budget_usd;
                  const utilPct = dp.budget_utilization_pct;
                  const exceeded = dp.budget_exceeded;
                  if (typeof budget === 'number' && budget > 0 && typeof utilPct === 'number') {
                    const spendStr = typeof spend === 'number' ? `$${spend.toFixed(2)}` : '?';
                    const tooltip = `${spendStr} / $${budget.toFixed(2)} budget (${Math.round(utilPct)}% utilized)`;
                    if (exceeded) {
                      return (
                        <span className="text-[9px] bg-red-500/10 text-red-400 border border-red-500/20 px-1.5 py-0.5 rounded font-mono" title={tooltip}>
                          over budget
                        </span>
                      );
                    }
                    if (utilPct >= 80) {
                      return (
                        <span className="text-[9px] bg-amber-500/10 text-amber-500 border border-amber-500/20 px-1.5 py-0.5 rounded font-mono" title={tooltip}>
                          {Math.round(utilPct)}% budget
                        </span>
                      );
                    }
                    return (
                      <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded font-mono" title={tooltip}>
                        {Math.round(utilPct)}% budget
                      </span>
                    );
                  }
                  if (typeof spend === 'number' && spend > 0) {
                    return (
                      <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded font-mono" title="Estimated cost this window (no budget set)">
                        ${spend.toFixed(2)}
                      </span>
                    );
                  }
                  return null;
                })()}
              </div>
            </button>
            {isExpanded && (
              <div className="mt-1 ml-5 flex flex-col gap-1">
                {dp.completion_criteria && (
                  <span className="text-[10px] text-neutral-400">Completion: {dp.completion_criteria}</span>
                )}
                {dp.completion_suggestion && (
                  <span className="text-[10px] text-neutral-500 italic">{dp.completion_suggestion}</span>
                )}
                {dp.recent_finding_titles && dp.recent_finding_titles.length > 0 && (
                  <div className="flex flex-col gap-0.5">
                    {dp.recent_finding_titles.slice(0, 3).map((title, i) => (
                      <span key={i} className="text-[10px] text-neutral-400">· {title}</span>
                    ))}
                  </div>
                )}
                {dp.recent_failure_titles && dp.recent_failure_titles.length > 0 && (
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[9px] text-red-400 uppercase tracking-wide">recent failures:</span>
                    {dp.recent_failure_titles.slice(0, 3).map((title, i) => (
                      <span key={i} className="text-[10px] text-red-300">· {title}</span>
                    ))}
                  </div>
                )}
                {dp.latest_insights && dp.latest_insights.length > 0 && (
                  <div className="flex flex-col gap-0.5">
                    {dp.latest_insights.slice(0, 2).map((insight, i) => (
                      <span key={i} className="text-[10px] text-neutral-500 italic">{insight}</span>
                    ))}
                  </div>
                )}
                {dp.latest_research_at && (
                  <span className="text-[9px] text-neutral-600">Last researched: {timeAgo(dp.latest_research_at)}</span>
                )}
                {dp.scope_breakdown && Object.keys(dp.scope_breakdown).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[9px] text-neutral-600 uppercase tracking-wide">scope:</span>
                    {Object.entries(dp.scope_breakdown).map(([scope, stats]) => {
                      const merged = stats?.merged ?? 0;
                      const created = stats?.created ?? 0;
                      const crossModule = stats?.merged_cross_module ?? 0;
                      const scopeOverflow = stats?.failed_scope_overflow ?? 0;
                      if (!created && !merged) return null;
                      const detail: string[] = [];
                      if (crossModule > 0) detail.push(`${crossModule} cross-dir`);
                      if (scopeOverflow > 0) detail.push(`${scopeOverflow} overflow`);
                      const expMerged = stats?.avg_expansion_ratio_merged;
                      const expFailed = stats?.avg_expansion_ratio_failed;
                      if (typeof expMerged === 'number' || typeof expFailed === 'number') {
                        const m = typeof expMerged === 'number' ? `${expMerged.toFixed(1)}x` : '--';
                        const f = typeof expFailed === 'number' ? `${expFailed.toFixed(1)}x` : '--';
                        detail.push(`exp ${m}/${f}`);
                      }
                      return (
                        <span key={scope} className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded">
                          {scope} {merged}/{created}
                          {detail.length > 0 && <span className="text-neutral-500"> ({detail.join(', ')})</span>}
                        </span>
                      );
                    })}
                  </div>
                )}
                {dp.type_breakdown && Object.keys(dp.type_breakdown).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[9px] text-neutral-600 uppercase tracking-wide">type:</span>
                    {Object.entries(dp.type_breakdown).map(([ttype, stats]) => {
                      const merged = stats?.merged ?? 0;
                      const created = stats?.created ?? 0;
                      if (!created && !merged) return null;
                      return (
                        <span key={ttype} className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded">
                          {ttype} {merged}/{created}
                        </span>
                      );
                    })}
                  </div>
                )}
                {dp.taxonomy_summary && Object.keys(dp.taxonomy_summary).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[9px] text-neutral-600 uppercase tracking-wide">failures:</span>
                    {TAXONOMY_ORDER.map(cat => {
                      const cnt = dp.taxonomy_summary?.[cat];
                      if (!cnt) return null;
                      return (
                        <span key={cat} className={`text-[9px] px-1.5 py-0.5 rounded border ${TAXONOMY_COLORS[cat]}`} title={TAXONOMY_LABELS[cat]}>
                          {TAXONOMY_LABELS[cat]} {cnt}
                        </span>
                      );
                    })}
                  </div>
                )}
                {dp.stage_dropout && Object.keys(dp.stage_dropout).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[9px] text-neutral-600 uppercase tracking-wide">stages:</span>
                    {STAGE_ORDER.map(stage => {
                      const cnt = dp.stage_dropout?.[stage];
                      if (!cnt) return null;
                      return (
                        <span key={stage} className={`text-[9px] px-1.5 py-0.5 rounded border ${STAGE_COLORS[stage] ?? 'bg-neutral-700/20 text-neutral-400 border-neutral-700/30'}`}>
                          {stage} {cnt}
                        </span>
                      );
                    })}
                  </div>
                )}
                {(() => {
                  const cycles = dp.lifetime_cycles ?? 0;
                  const merged = dp.lifetime_prs_merged ?? 0;
                  const failed = dp.lifetime_tasks_failed ?? 0;
                  const rejected = dp.lifetime_prs_rejected ?? 0;
                  if (!cycles && !merged && !failed && !rejected) return null;
                  const conv = typeof dp.lifetime_conversion_rate === 'number'
                    ? `${Math.round(dp.lifetime_conversion_rate * 100)}%`
                    : '—';
                  return (
                    <span className="text-[9px] text-neutral-500">
                      Lifetime: {cycles} cycle{cycles !== 1 ? 's' : ''} · {merged} merged · {failed} failed · {conv} conversion
                    </span>
                  );
                })()}
                {(() => {
                  const rq = dp.review_quality;
                  if (!rq) return null;
                  const dismissed = rq.reviews_dismissed ?? 0;
                  const overridden = rq.reviews_overridden ?? 0;
                  const caught = rq.user_issues_caught ?? 0;
                  if (!dismissed && !overridden && !caught) return null;
                  const parts: string[] = [];
                  if (dismissed) parts.push(`${dismissed} dismissed`);
                  if (overridden) parts.push(`${overridden} overridden`);
                  if (caught) parts.push(`${caught} issues caught`);
                  return (
                    <span className="text-[9px] text-neutral-500">
                      Review feedback: {parts.join(' · ')}
                    </span>
                  );
                })()}
                {(() => {
                  const ff = dp.filter_funnel;
                  if (!ff) return null;
                  const produced = ff.findings_produced ?? 0;
                  const created = ff.tasks_created ?? 0;
                  if (!produced && !created) return null;
                  const filters: string[] = [];
                  if (ff.hash_dedup) filters.push(`${ff.hash_dedup} hash`);
                  if (ff.word_overlap) filters.push(`${ff.word_overlap} word`);
                  if (ff.file_overlap) filters.push(`${ff.file_overlap} file`);
                  if (ff.type_filtered) filters.push(`${ff.type_filtered} type`);
                  if (ff.empty_prompt) filters.push(`${ff.empty_prompt} empty`);
                  if (ff.batch_cap) filters.push(`${ff.batch_cap} cap`);
                  const conv = produced > 0 ? ` (${Math.round((created / produced) * 100)}%)` : '';
                  return (
                    <span className="text-[9px] text-neutral-500">
                      Funnel: {produced} findings → {created} tasks{conv}
                      {filters.length > 0 ? ` · filtered: ${filters.join(' · ')}` : ''}
                    </span>
                  );
                })()}
                {dp.prompt_quality_hint && (
                  <span className="text-[10px] text-neutral-500 italic" title="Synthesized from merged vs failed prompt telemetry">
                    Prompt quality: {dp.prompt_quality_hint}
                  </span>
                )}
                {(() => {
                  const ds = dp.decomposition_summary;
                  if (!ds || !ds.plans_assessed) return null;
                  const rate = typeof ds.avg_subtask_merge_rate === 'number'
                    ? `${Math.round(ds.avg_subtask_merge_rate * 100)}%`
                    : '—';
                  const count = typeof ds.avg_subtask_count === 'number' ? ds.avg_subtask_count : null;
                  return (
                    <span className="text-[9px] text-neutral-500">
                      Plan quality: {rate} subtask merge rate across {ds.plans_assessed} plan{ds.plans_assessed !== 1 ? 's' : ''}
                      {count !== null ? ` · avg ${count} subtasks` : ''}
                    </span>
                  );
                })()}
                {(() => {
                  const total = dp.estimated_cost_usd ?? 0;
                  if (!total) return null;
                  const breakdown = dp.cost_by_scope
                    ? Object.entries(dp.cost_by_scope)
                        .filter(([, v]) => typeof v === 'number' && v > 0)
                        .sort((a, b) => b[1] - a[1])
                        .map(([scope, v]) => `${scope} $${v.toFixed(2)}`)
                        .join(' · ')
                    : '';
                  const typeBreakdown = dp.cost_by_type
                    ? Object.entries(dp.cost_by_type)
                        .filter(([, v]) => typeof v === 'number' && v > 0)
                        .sort((a, b) => b[1] - a[1])
                        .map(([t, v]) => `${t} $${v.toFixed(2)}`)
                        .join(' · ')
                    : '';
                  return (
                    <>
                      <span className="text-[9px] text-neutral-500 font-mono">
                        Cost: ${total.toFixed(2)}{breakdown ? ` — ${breakdown}` : ''}
                      </span>
                      {typeBreakdown && (
                        <span className="text-[9px] text-neutral-500 font-mono">
                          By type: {typeBreakdown}
                        </span>
                      )}
                      {dp.cost_by_status && (() => {
                        const entries = Object.entries(dp.cost_by_status)
                          .filter(([, v]) => typeof v === 'number' && v > 0)
                          .sort((a, b) => b[1] - a[1]);
                        if (entries.length === 0) return null;
                        const failedAmt = dp.cost_by_status.FAILED ?? 0;
                        const failedHigh = total > 0 && failedAmt / total > 0.3;
                        return (
                          <span className="text-[9px] text-neutral-500 font-mono">
                            By status:{' '}
                            {entries.map(([status, v], i) => {
                              const isFailed = status === 'FAILED';
                              const highlight = isFailed && failedHigh;
                              return (
                                <span
                                  key={status}
                                  className={highlight ? 'text-amber-600 font-semibold' : ''}
                                >
                                  {i > 0 ? ' · ' : ''}
                                  {status} ${v.toFixed(2)}
                                </span>
                              );
                            })}
                          </span>
                        );
                      })()}
                    </>
                  );
                })()}
                {dp.rejection_reasons && dp.rejection_reasons.length > 0 && (
                  <div className="flex flex-col gap-0.5">
                    <span className="text-[9px] text-neutral-600 uppercase tracking-wide">rejections:</span>
                    {dp.rejection_reasons.slice(0, 3).map((r, i) => (
                      <span key={i} className="text-[10px] text-neutral-500 italic truncate" title={r.rejection_note || ''}>
                        · {(r.rejection_note || r.title || '').slice(0, 120)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
