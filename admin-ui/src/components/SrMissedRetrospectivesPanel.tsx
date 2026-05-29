import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { AlertTriangle, CheckCircle, ChevronDown, ChevronRight, RefreshCw, Search, X } from 'lucide-react';
import { fetchSrCatchRateHealth, fetchSrMissedRetrospectives, type SrCatchRateHealthResponse } from '../api';
import type { SrMissedTask } from './SrTrendBreakdown';
import { SR_CANONICAL_CATEGORIES, SrCatchRateFreshnessPill } from './SrTrendBreakdown';
import { timeAgo } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import { useUrlFilterState } from '../hooks/useUrlFilterState';

const PAGE_SIZE = 20;
const DAY_OPTIONS = [7, 14, 30, 90];
const SCOPE_OPTIONS = ['small', 'medium', 'large'] as const;
// Reviewer-finding severity scale (highest → lowest), mirroring the backend
// _SEVERITY_RANK in sr_diagnostics_endpoints.py.
const SEVERITY_OPTIONS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const;

export interface SrMissedRetrospectivesPanelProps {
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
  activeCategory?: string;
  activeScope?: string;
  activeInvestigationTaskIds?: Record<string, string> | null;
}

export function SrMissedRetrospectivesPanel({ selectedProjectId, onSelectTask, activeCategory, activeScope, activeInvestigationTaskIds }: SrMissedRetrospectivesPanelProps) {
  const [items, setItems] = useState<SrMissedTask[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const {
    days,
    secondaryFilter: categoryFilter,
    taskTypeFilter,
    scopeFilter,
    severityFilter,
    page,
    setDays,
    setSecondaryFilter: setCategoryFilter,
    setTaskTypeFilter,
    setScopeFilter,
    setSeverityFilter,
    setPage,
  } = useUrlFilterState('srm_', selectedProjectId, { withScope: true, withSeverity: true });
  const [stableTaskTypes, setStableTaskTypes] = useState<string[]>([]);
  const [categoryCounts, setCategoryCounts] = useState<Record<string, number>>({});
  const [catchRateHealth, setCatchRateHealth] = useState<SrCatchRateHealthResponse | null>(null);
  const [healthDismissed, setHealthDismissed] = useState(false);
  const taskTypesAccRef = useRef<Set<string>>(new Set());
  const catchRateHealthCacheRef = useRef(new Map<string, Promise<SrCatchRateHealthResponse>>());
  const mountedRef = useRef(true);
  const panelMountTimeRef = useRef<number>(Date.now());
  const abortControllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const fetchData = useCallback(async () => {
    if (!selectedProjectId) return;
    // Cancel any in-flight request from a previous filter state
    abortControllerRef.current?.abort();
    const controller = new AbortController();
    abortControllerRef.current = controller;
    const currentId = ++requestIdRef.current;

    setLoading(true);
    setFetchError(null);
    try {
      const result = await fetchSrMissedRetrospectives(selectedProjectId, {
        days,
        category: categoryFilter || undefined,
        task_type: taskTypeFilter || undefined,
        scope: scopeFilter || undefined,
        min_severity: severityFilter || undefined,
        page: page - 1,  // backend is 0-indexed; UI is 1-indexed
        page_size: PAGE_SIZE,
      }, controller.signal);
      // Discard stale responses: mountedRef guards unmount, requestIdRef guards filter-change races
      if (!mountedRef.current || currentId !== requestIdRef.current) return;
      setItems(result.items);
      setTotal(result.total);
      const serverTypes = (result as { task_types?: string[] }).task_types;
      if (serverTypes) {
        setStableTaskTypes(serverTypes.slice().sort());
      } else {
        result.items.forEach(t => { if (t.task_type) taskTypesAccRef.current.add(t.task_type); });
        setStableTaskTypes(Array.from(taskTypesAccRef.current).sort());
      }
      if (result.category_counts) {
        setCategoryCounts(result.category_counts);
      }
    } catch (e) {
      // Silently discard aborted requests — the panel already issued a newer fetch
      if (e instanceof Error && e.name === 'AbortError') return;
      if (!mountedRef.current || currentId !== requestIdRef.current) return;
      setFetchError(e instanceof Error ? e.message : 'network error');
    } finally {
      if (mountedRef.current && currentId === requestIdRef.current) setLoading(false);
    }
  }, [selectedProjectId, days, categoryFilter, taskTypeFilter, scopeFilter, severityFilter, page]);

  useEffect(() => {
    if (!selectedProjectId) {
      setCatchRateHealth(null);
      return;
    }

    // Re-show the dismissable health banner whenever the project or day window changes
    setHealthDismissed(false);
    const cacheKey = `${selectedProjectId}:${days}`;
    const cached = catchRateHealthCacheRef.current.get(cacheKey);
    let active = true;
    const request = cached ?? fetchSrCatchRateHealth(selectedProjectId);
    if (!cached) catchRateHealthCacheRef.current.set(cacheKey, request);

    request.then((result) => {
      if (active) setCatchRateHealth(result);
    }).catch(() => {
      if (active) setCatchRateHealth(null);
      catchRateHealthCacheRef.current.delete(cacheKey);
    });

    return () => {
      active = false;
    };
  }, [selectedProjectId, days]);

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    return () => {
      mountedRef.current = false;
      abortControllerRef.current?.abort();
    };
  }, [fetchData]);

  // Sync external activeCategory prop into internal categoryFilter
  useEffect(() => {
    if (activeCategory !== undefined) setCategoryFilter(activeCategory);
  }, [activeCategory, setCategoryFilter]);

  // Sync external activeScope prop into internal scopeFilter
  useEffect(() => {
    if (activeScope !== undefined) setScopeFilter(activeScope);
  }, [activeScope, setScopeFilter]);

  // Reset page when filters change
  const prevFiltersRef = useRef({ days, categoryFilter, taskTypeFilter, scopeFilter, severityFilter, selectedProjectId });
  useEffect(() => {
    const prev = prevFiltersRef.current;
    if (prev.days !== days || prev.categoryFilter !== categoryFilter || prev.taskTypeFilter !== taskTypeFilter || prev.scopeFilter !== scopeFilter || prev.severityFilter !== severityFilter || prev.selectedProjectId !== selectedProjectId) {
      setPage(1);
      taskTypesAccRef.current = new Set();
      setStableTaskTypes([]);
      setCategoryCounts({});
      prevFiltersRef.current = { days, categoryFilter, taskTypeFilter, scopeFilter, severityFilter, selectedProjectId };
    }
  }, [days, categoryFilter, taskTypeFilter, scopeFilter, severityFilter, selectedProjectId, setPage]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const isStale = Date.now() - panelMountTimeRef.current > 300_000; // 5 minutes
  const handleRefresh = useCallback(() => {
    panelMountTimeRef.current = Date.now();
    fetchData();
  }, [fetchData]);

  const catFreq = useMemo(
    () => Object.entries(categoryCounts).sort((a, b) => b[1] - a[1]),
    [categoryCounts]
  );

  if (!selectedProjectId) return null;

  return (
    <section aria-label="SR missed retrospectives" data-testid="sr-missed-retrospectives-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">SR Missed Retrospectives</h2>

      <div className="flex flex-wrap items-center gap-2 mb-2 text-[10px]">
        {DAY_OPTIONS.map(d => (
          // eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels
          <button
            key={d}
            type="button"
            onClick={() => setDays(d)}
            className={`px-1.5 py-0.5 rounded border ${days === d ? 'border-blue-500 text-blue-300' : 'border-neutral-700 text-neutral-400 hover:border-neutral-500'}`}
          >{d}d</button>
        ))}
        <select
          value={categoryFilter}
          onChange={e => setCategoryFilter(e.target.value)}
          className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300 text-[10px]"
          aria-label="Filter by category"
        >
          <option value="">All categories</option>
          {SR_CANONICAL_CATEGORIES.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}
        </select>
        {stableTaskTypes.length > 0 && (
          <select
            value={taskTypeFilter}
            onChange={e => setTaskTypeFilter(e.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300 text-[10px]"
            aria-label="Filter by task type"
          >
            <option value="">All types</option>
            {stableTaskTypes.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <select
          value={scopeFilter}
          onChange={e => setScopeFilter(e.target.value)}
          className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300 text-[10px]"
          aria-label="Filter by scope"
        >
          <option value="">All scopes</option>
          {SCOPE_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select
          value={severityFilter}
          onChange={e => setSeverityFilter(e.target.value)}
          className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300 text-[10px]"
          aria-label="Filter by minimum severity"
        >
          <option value="">Any severity</option>
          {SEVERITY_OPTIONS.map(s => <option key={s} value={s}>{s}+</option>)}
        </select>
        <SrCatchRateFreshnessPill health={catchRateHealth} />
      </div>

      {catchRateHealth && catchRateHealth.status !== 'healthy' && !healthDismissed && (
        <SrHealthBanner health={catchRateHealth} onDismiss={() => setHealthDismissed(true)} />
      )}

      {loading ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 h-14 animate-pulse" />
      ) : fetchError ? (
        <div
          className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2"
          role="status"
        >
          <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
          <span>Error fetching SR data</span>
          <span className="text-[10px] text-neutral-600 truncate" title={fetchError}>{fetchError}</span>
        </div>
      ) : items.length === 0 ? (
        categoryFilter || taskTypeFilter || scopeFilter || severityFilter ? (
          <div
            className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2"
            role="status"
            data-testid="sr-missed-empty"
          >
            <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
            <span>
              No misses for{categoryFilter ? ` category "${categoryFilter}"` : ''}{taskTypeFilter ? ` type "${taskTypeFilter}"` : ''}{scopeFilter ? ` scope "${scopeFilter}"` : ''}{severityFilter ? ` severity "${severityFilter}+"` : ''}.{' '}
              {/* eslint-disable-next-line viewport/min-touch-target -- inline text link, dense context */}
              <button type="button" className="underline hover:text-neutral-300" onClick={() => { setCategoryFilter(''); setTaskTypeFilter(''); setScopeFilter(''); setSeverityFilter(''); }}>Clear filters</button>
            </span>
          </div>
        ) : (
          <div
            className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-emerald-400 flex items-center gap-2"
            role="status"
            data-testid="sr-missed-empty"
          >
            <CheckCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
            <span>No SR misses in the last {days} days — self-review is catching everything</span>
          </div>
        )
      ) : (
        <>
          {catFreq.length > 0 && (
            <>
              {categoryFilter && (
                <div className="text-[9px] text-neutral-500 mb-1">missed alongside {categoryFilter}</div>
              )}
              <div className="flex flex-wrap gap-1.5 mb-2">
                {catFreq.map(([cat, count]) => {
                const investigationTaskId = activeInvestigationTaskIds?.[cat];
                return (
                  <span key={cat} className="inline-flex items-center gap-0.5">
                    {/* eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels */}
                    <button
                      type="button"
                      onClick={() => setCategoryFilter(cat === categoryFilter ? '' : cat)}
                      className={`px-1.5 py-0.5 rounded text-[9px] border transition-colors ${cat === categoryFilter ? 'border-red-500/60 bg-red-950/30 text-red-300' : 'border-neutral-700 text-neutral-400 hover:border-red-500/40'}`}
                      title={`Filter by ${cat}`}
                    >
                      {cat} <span className="text-neutral-500">{count}</span>
                    </button>
                    {investigationTaskId && (
                      // eslint-disable-next-line viewport/min-touch-target -- dense inline indicator
                      <button
                        type="button"
                        onClick={() => onSelectTask(investigationTaskId)}
                        title={`Open SR investigation: ${investigationTaskId}`}
                        aria-label={`Open SR investigation for ${cat}: ${investigationTaskId}`}
                        data-testid={`investigation-badge-${cat}`}
                        className="text-cyan-600 hover:text-cyan-400 shrink-0"
                      >
                        <Search className="w-2.5 h-2.5" />
                      </button>
                    )}
                  </span>
                );
              })}
              <span className="text-[9px] text-neutral-600 self-center ml-1">{total} total</span>
              </div>
              </>
            )}
          <div className="flex flex-col gap-1">
            {items.map(t => (
              <SrMissRow key={t.task_id} task={t} onClick={() => onSelectTask(t.task_id)} investigationTaskIds={activeInvestigationTaskIds} onOpenInvestigation={onSelectTask} />
            ))}
          </div>
          {totalPages > 1 && (
            <div className="flex flex-wrap items-center gap-2 mt-2 text-[10px] text-neutral-400">
              {/* eslint-disable-next-line viewport/min-touch-target -- compact pagination, consistent with adjacent panels */}
              <button
                type="button"
                disabled={page === 1}
                onClick={() => setPage(p => Math.max(1, p - 1))}
                className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
                aria-label="Previous page"
              >←</button>
              <span>
                Page {page} of {totalPages} · {total} total
                {total > 0 && ` · showing ${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)}`}
              </span>
              {/* eslint-disable-next-line viewport/min-touch-target -- compact pagination, consistent with adjacent panels */}
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
                aria-label="Next page"
              >→</button>
            </div>
          )}
          {isStale && (
            // eslint-disable-next-line viewport/min-touch-target -- dense control
            <button
              type="button"
              onClick={handleRefresh}
              disabled={loading}
              className="mt-1 p-1 text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
              title="Refresh data (window may have moved)"
              aria-label="Refresh pagination data"
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          )}
        </>
      )}
    </section>
  );
}

const HEALTH_TOOLTIP: Record<string, string> = {
  stale: 'The SR catch-rate snapshot has not been refreshed recently. Displayed metrics may lag reality.',
  frozen: 'The signal clock has stopped: catch-rate was not recomputed even though the signal timestamp advanced. Metrics are likely incorrect.',
  low_data: 'Not enough tasks have been reviewed to compute a reliable catch rate. Treat metrics as preliminary.',
  never_sufficient: 'The signal has been computed but never reached sufficient data to report a catch rate.',
  missing: 'No SR catch-rate snapshot has been computed yet for this project.',
};

function SrHealthBanner({ health, onDismiss }: { health: SrCatchRateHealthResponse; onDismiss: () => void }) {
  const isRed = health.status === 'frozen' || health.status === 'never_sufficient' || health.status === 'missing';
  const isAmber = health.status === 'stale';
  const colorClass = isRed
    ? 'border-red-800/40 bg-red-950/20 text-red-300'
    : isAmber
      ? 'border-amber-800/40 bg-amber-950/20 text-amber-300'
      : 'border-neutral-700 bg-neutral-900/50 text-neutral-400';
  const iconColor = isRed ? 'text-red-400' : isAmber ? 'text-amber-400' : 'text-neutral-500';

  let ageText = '';
  if (health.status === 'stale' || health.status === 'frozen') {
    if (health.last_sufficient_age_hours !== null) {
      ageText = ` · last sufficient ${Math.round(health.last_sufficient_age_hours)}h ago`;
    }
  } else if (health.status === 'never_sufficient') {
    if (health.signal_age_hours !== null) {
      ageText = ` · signal ${Math.round(health.signal_age_hours)}h ago`;
    }
  }

  const tooltip = HEALTH_TOOLTIP[health.status] ?? '';

  return (
    <div
      className={`flex items-center gap-1.5 mb-2 px-2 py-1 rounded border text-[10px] ${colorClass}`}
      role="status"
      data-testid="sr-health-banner"
      title={tooltip}
    >
      <AlertTriangle className={`w-3 h-3 shrink-0 ${iconColor}`} aria-hidden="true" />
      <span className="flex-1">{health.status}{ageText}</span>
      {/* eslint-disable-next-line viewport/min-touch-target -- dense dismiss control */}
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 hover:opacity-70"
        aria-label="Dismiss health banner"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

function SrMissRow({ task, onClick, investigationTaskIds, onOpenInvestigation }: { task: SrMissedTask; onClick: () => void; investigationTaskIds?: Record<string, string> | null; onOpenInvestigation?: (taskId: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    // eslint-disable-next-line viewport/min-touch-target -- dense row, consistent with SrMissedRetrospective
    <div
      className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-[10px] cursor-pointer hover:border-neutral-600"
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } }}
      aria-label={`View task: ${task.title}`}
    >
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          className="text-neutral-500 hover:text-neutral-300 shrink-0"
          aria-label={`${expanded ? 'collapse' : 'expand'} ${task.title}`}
          aria-expanded={expanded}
          onClick={e => { e.stopPropagation(); setExpanded(v => !v); }}
        >
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        </button>
        <span className="text-neutral-200 truncate flex-1 font-medium" title={task.title}>{task.title}</span>
        {task.task_type && <span className="text-neutral-500 shrink-0">{task.task_type}</span>}
        {task.created_at && (
          <span className="text-neutral-600 shrink-0" title={task.created_at}>{timeAgo(task.created_at)}</span>
        )}
      </div>
      <div className="flex flex-wrap gap-x-2 gap-y-0.5 mt-0.5 pl-4">
        {task.missed_categories.map(cat => (
          <span key={cat} className="bg-red-950/40 border border-red-800/30 rounded px-1 py-0.5 text-red-300 text-[9px]">{cat}</span>
        ))}
        {task.caught_categories.length > 0 && (
          <span className="text-neutral-600 text-[9px]">{task.caught_categories.length} caught</span>
        )}
        {task.scope && <span className="text-neutral-600 text-[9px]">{task.scope}</span>}
        {task.pr_url && isSafeUrl(task.pr_url) && (
          // eslint-disable-next-line viewport/min-touch-target -- dense inline link
          <a href={task.pr_url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="text-blue-400 hover:underline text-[9px]">PR</a>
        )}
      </div>
      {expanded && task.missed_categories.length > 0 && (
        <div className="mt-1.5 pl-4 flex flex-col gap-1">
          {task.missed_categories.map(cat => {
            const sr = task.sr_check_results?.[cat];
            const reviewer = task.reviewer_findings?.find(f => f.category === cat);
            const investigationTaskId = investigationTaskIds?.[cat];
            return (
              <div key={cat} className="rounded border border-neutral-800/60 bg-neutral-950/50 p-1.5">
                <div className="mb-1 flex items-center gap-1">
                  <span className="text-neutral-400 font-medium">{cat}</span>
                  {investigationTaskId && onOpenInvestigation && (
                    // eslint-disable-next-line viewport/min-touch-target -- dense inline indicator
                    <button
                      type="button"
                      onClick={e => { e.stopPropagation(); onOpenInvestigation(investigationTaskId); }}
                      title={`Open SR investigation: ${investigationTaskId}`}
                      aria-label={`Open SR investigation for ${cat}: ${investigationTaskId}`}
                      data-testid={`investigation-drawer-${cat}`}
                      className="text-cyan-600 hover:text-cyan-400 shrink-0"
                    >
                      <Search className="w-2.5 h-2.5" />
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-x-3 text-[9px] mb-0.5">
                  <span>
                    <span className="text-neutral-500">SR: </span>
                    {sr ? (
                      <span className={sr.passed ? 'text-emerald-400' : 'text-red-400'}>{sr.passed ? 'passed' : 'failed'}</span>
                    ) : <span className="text-neutral-600">—</span>}
                  </span>
                  <span>
                    <span className="text-neutral-500">Reviewer: </span>
                    {reviewer ? (
                      <span className="text-amber-400">{reviewer.severity ?? 'flagged'}</span>
                    ) : <span className="text-neutral-600">—</span>}
                  </span>
                </div>
                {reviewer?.description && (
                  <div className="text-[9px] text-neutral-300 leading-relaxed">{reviewer.description}</div>
                )}
                {sr && !sr.passed && sr.issue && (
                  <div className="text-[9px] text-neutral-500 leading-relaxed italic mt-0.5">{sr.issue}</div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
