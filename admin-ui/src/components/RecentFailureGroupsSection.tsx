import { useState } from 'react';
import { ChevronDown, ChevronUp, XCircle } from 'lucide-react';
import { TAXONOMY_LABELS, TAXONOMY_COLORS } from './taxonomyConstants';
import { TIMEOUT_SUBTYPE_LABELS } from './catchupHelpers';

interface FailureGroupTask {
  task_id: string;
  title: string;
  error_snippet: string | null;
  attempt_count: number;
  waiting_hours?: number;
  taxonomy?: string;
  timeout_subtype?: string | null;
  failure_stage?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
}

interface FailureGroup {
  failure_class: string;
  label: string;
  count: number;
  tasks: FailureGroupTask[];
}

type TrendPeriod = {
  period_end: string;
  success_rate: number | null;
  completed: number;
  failed: number;
  failed_by_taxonomy?: Record<string, number>;
  failed_by_class?: Record<string, number>;
  first_attempt_success_rate?: number | null;
  period_cost_usd?: number | null;
};

interface RecentFailureGroupsSectionProps {
  extraFailureGroups: FailureGroup[];
  activeTaxonomyFilter: string | null;
  activeStageFilter: string | null;
  trendHistory: TrendPeriod[] | undefined;
  costByFailureClass: Record<string, number> | null | undefined;
  expandedSnippets: Set<string>;
  onToggleSnippet: (id: string) => void;
  onSelectTask: (task: { id: string }) => void;
  failuresExpanded: boolean;
  onToggleFailuresExpanded: () => void;
}

export function RecentFailureGroupsSection({
  extraFailureGroups,
  activeTaxonomyFilter,
  activeStageFilter,
  trendHistory,
  costByFailureClass,
  expandedSnippets,
  onToggleSnippet,
  onSelectTask,
  failuresExpanded,
  onToggleFailuresExpanded,
}: RecentFailureGroupsSectionProps) {
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  if (extraFailureGroups.length === 0) return null;

  const filteredGroups = (activeTaxonomyFilter || activeStageFilter)
    ? extraFailureGroups
        .map(g => ({
          ...g,
          tasks: g.tasks.filter(t =>
            (!activeTaxonomyFilter || t.taxonomy === activeTaxonomyFilter) &&
            (!activeStageFilter || t.failure_stage === activeStageFilter)
          ),
        }))
        .filter(g => g.tasks.length > 0)
    : extraFailureGroups;
  const totalCount = filteredGroups.reduce((s, g) => s + g.tasks.length, 0);
  const filterLabel = activeTaxonomyFilter
    ? `${TAXONOMY_LABELS[activeTaxonomyFilter] ?? activeTaxonomyFilter}`
    : activeStageFilter ?? null;
  const classHistory = trendHistory ?? [];

  return (
    <div>
      <button
        onClick={onToggleFailuresExpanded}
        aria-expanded={failuresExpanded}
        className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
      >
        {failuresExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <XCircle className="w-3 h-3 text-red-500" />
        Recent failures ({totalCount}{filterLabel ? ` filtered: ${filterLabel}` : ''})
      </button>
      {failuresExpanded && (
        <div className="flex flex-col gap-2">
          {filteredGroups.length === 0 && (
            <div className="text-[11px] text-neutral-500 italic">No failures match the active filter.</div>
          )}
          {filteredGroups.map(group => {
            const isGroupExpanded = expandedGroups.has(group.failure_class);
            const badgeClass = group.failure_class === 'CODE_ERROR'
              ? 'bg-red-500/10 text-red-400 border-red-500/20'
              : group.failure_class === 'RATE_LIMIT' || group.failure_class === 'EXTERNAL_API'
              ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
              : 'bg-neutral-700/20 text-neutral-400 border-neutral-700/30';
            let classTrend: {arrow: string; color: string} = {arrow: '–', color: 'text-neutral-500'};
            if (classHistory.length >= 4) {
              const sumAt = (arr: typeof classHistory) => arr.reduce((a, p) => a + (p.failed_by_class?.[group.failure_class] ?? 0), 0);
              const recent = sumAt(classHistory.slice(-2));
              const prior = sumAt(classHistory.slice(-4, -2));
              if (recent > prior) classTrend = {arrow: '↑', color: 'text-red-400'};
              else if (recent < prior) classTrend = {arrow: '↓', color: 'text-emerald-400'};
            }
            return (
              <div key={group.failure_class}>
                <button
                  onClick={() => setExpandedGroups(prev => {
                    const next = new Set(prev);
                    if (next.has(group.failure_class)) next.delete(group.failure_class);
                    else next.add(group.failure_class);
                    return next;
                  })}
                  aria-expanded={isGroupExpanded}
                  className="flex flex-wrap items-center gap-2 w-full text-left hover:opacity-80 transition-opacity mb-1"
                >
                  {isGroupExpanded ? <ChevronUp className="w-3 h-3 text-neutral-500 shrink-0" /> : <ChevronDown className="w-3 h-3 text-neutral-500 shrink-0" />}
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${badgeClass}`}>
                    {group.label}
                  </span>
                  <span className="text-[11px] text-neutral-500">× {group.tasks.length}</span>
                  <span className={`text-[11px] font-bold ${classTrend.color}`} title="Trend: recent 2 periods vs prior 2 periods">{classTrend.arrow}</span>
                  {costByFailureClass?.[group.failure_class] != null && (costByFailureClass[group.failure_class] ?? 0) > 0 && (
                    <span className="text-[10px] text-red-400/70 font-mono" title={`Cost of ${group.label} failures`}>
                      ${(costByFailureClass[group.failure_class] ?? 0).toFixed(2)}
                    </span>
                  )}
                </button>
                {isGroupExpanded && (
                  <div className="flex flex-col gap-1 ml-5 max-w-full">
                    {group.tasks.map(t => (
                      <div key={t.task_id} className="flex flex-col gap-0.5">
                        <div
                          role="button"
                          tabIndex={0}
                          className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50 min-h-[44px]"
                          onClick={() => onSelectTask({ id: t.task_id })}
                          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: t.task_id }); } }}
                        >
                          {t.taxonomy && TAXONOMY_LABELS[t.taxonomy] && (
                            <span className={`shrink-0 text-[9px] leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[t.taxonomy] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30'}`} title={TAXONOMY_LABELS[t.taxonomy]}>
                              {TAXONOMY_LABELS[t.taxonomy]}
                            </span>
                          )}
                          <span className="flex-1 truncate text-neutral-400">{t.title}</span>
                          {t.direction_text && (
                            <span
                              className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[9rem] sm:max-w-[12rem]"
                              title={t.direction_text}
                            >
                              {t.direction_text.length > 40 ? t.direction_text.slice(0, 40) + '…' : t.direction_text}
                            </span>
                          )}
                          {t.failure_stage && (
                            <span
                              className="shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40"
                              title={`Failure stage: ${t.failure_stage}`}
                            >
                              {t.failure_stage}
                            </span>
                          )}
                          {group.failure_class === 'AGENT_TIMEOUT' && t.timeout_subtype && (
                            <span
                              className="shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400/80 border border-amber-500/20"
                              title={`Timeout subtype: ${t.timeout_subtype}`}
                            >
                              {TIMEOUT_SUBTYPE_LABELS[t.timeout_subtype] ?? t.timeout_subtype}
                            </span>
                          )}
                          {t.attempt_count > 1 && (
                            <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-500 border border-neutral-700/40">
                              attempt {t.attempt_count}
                            </span>
                          )}
                          {t.waiting_hours != null && t.waiting_hours > 0 && (
                            <span className="shrink-0 text-[10px] text-neutral-600 font-mono">{t.waiting_hours}h ago</span>
                          )}
                        </div>
                        {t.error_snippet && (
                          <div className="px-2">
                            <button
                              onClick={() => onToggleSnippet(t.task_id)}
                              aria-expanded={expandedSnippets.has(t.task_id)}
                              aria-label={expandedSnippets.has(t.task_id) ? 'Hide error details' : 'Show error details'}
                              className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
                            >
                              {expandedSnippets.has(t.task_id) ? 'hide error ▲' : 'show error ▼'}
                            </button>
                            {expandedSnippets.has(t.task_id) && (
                              <pre className="mt-1 text-[10px] text-red-400/80 bg-red-950/20 border border-red-900/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
                                {t.error_snippet}
                              </pre>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
