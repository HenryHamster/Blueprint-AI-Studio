/* eslint-disable react-hooks/purity, react-hooks/refs, react-hooks/set-state-in-effect */
import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { AlertTriangle, CheckCircle, ChevronDown, ChevronRight } from 'lucide-react';
import { fetchSrUnhandledEdgesRetrospectives, fetchSrCatchRateHealth, type SrUnhandledEdgesTask, type SrUnhandledEdge, type SrCatchRateHealth } from '../api';
import {
  RetrospectiveDayWindowButtons,
  RetrospectivePagination,
  RetrospectiveRefreshButton,
  SrCatchRateHealthBanner,
} from './SrRetrospectiveShared';
import { timeAgo } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import { useUrlFilterState } from '../hooks/useUrlFilterState';

const PAGE_SIZE = 20;
const BLIND_SPOT_PILL_MIN_COUNT = 2;
const BLIND_SPOT_PILL_MIN_THEMES = 2;
const BLIND_SPOT_PILL_CAP = 8;

export interface SrUnhandledEdgesRetrospectivesPanelProps {
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
}

export function SrUnhandledEdgesRetrospectivesPanel({ selectedProjectId, onSelectTask }: SrUnhandledEdgesRetrospectivesPanelProps) {
  const [items, setItems] = useState<SrUnhandledEdgesTask[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const {
    days,
    secondaryFilter: blindSpotFilter,
    tertiaryFilter: casePatternFilter,
    taskTypeFilter,
    page,
    setDays,
    setSecondaryFilter: setBlindSpotFilter,
    setTertiaryFilter: setCasePatternFilter,
    setTaskTypeFilter,
    setPage,
  } = useUrlFilterState('sru_', selectedProjectId, { secondaryParam: 'blind_spot', tertiaryParam: 'case_pattern' });
  const [stableTaskTypes, setStableTaskTypes] = useState<string[]>([]);
  const [blindSpotCounts, setBlindSpotCounts] = useState<Record<string, number>>({});
  const [edgeCaseCounts, setEdgeCaseCounts] = useState<Record<string, number>>({});
  const [health, setHealth] = useState<SrCatchRateHealth | null>(null);
  const [healthDismissed, setHealthDismissed] = useState(false);
  const taskTypesAccRef = useRef<Set<string>>(new Set());
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
      const result = await fetchSrUnhandledEdgesRetrospectives(selectedProjectId, {
        days,
        task_type: taskTypeFilter || undefined,
        blind_spot: blindSpotFilter || undefined,
        case_pattern: casePatternFilter || undefined,
        page: page - 1,  // backend is 0-indexed; UI is 1-indexed
        page_size: PAGE_SIZE,
      }, controller.signal);
      // Discard stale responses: mountedRef guards unmount, requestIdRef guards filter-change races
      if (!mountedRef.current || currentId !== requestIdRef.current) return;
      setItems(result.items);
      setTotal(result.total);
      if (result.task_types) {
        setStableTaskTypes(result.task_types.slice().sort());
      } else {
        result.items.forEach(t => { if (t.task_type) taskTypesAccRef.current.add(t.task_type); });
        setStableTaskTypes(Array.from(taskTypesAccRef.current).sort());
      }
      setBlindSpotCounts(result.blind_spot_counts ?? {});
      setEdgeCaseCounts(result.edge_case_counts ?? {});
    } catch (e) {
      // Silently discard aborted requests — the panel already issued a newer fetch
      if (e instanceof Error && e.name === 'AbortError') return;
      if (!mountedRef.current || currentId !== requestIdRef.current) return;
      setFetchError(e instanceof Error ? e.message : 'network error');
    } finally {
      if (mountedRef.current && currentId === requestIdRef.current) setLoading(false);
    }
  }, [selectedProjectId, days, taskTypeFilter, blindSpotFilter, casePatternFilter, page]);

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    return () => {
      mountedRef.current = false;
      abortControllerRef.current?.abort();
    };
  }, [fetchData]);

  useEffect(() => {
    if (!selectedProjectId) return;
    setHealth(null);
    setHealthDismissed(false);
    let cancelled = false;
    fetchSrCatchRateHealth(selectedProjectId).then(h => {
      if (!cancelled) setHealth(h);
    }).catch(() => { /* non-critical, silently ignore */ });
    return () => { cancelled = true; };
  }, [selectedProjectId]);

  // Reset page when filters change
  const prevFiltersRef = useRef({ days, taskTypeFilter, blindSpotFilter, casePatternFilter, selectedProjectId });
  useEffect(() => {
    const prev = prevFiltersRef.current;
    if (
      prev.days !== days ||
      prev.taskTypeFilter !== taskTypeFilter ||
      prev.blindSpotFilter !== blindSpotFilter ||
      prev.casePatternFilter !== casePatternFilter ||
      prev.selectedProjectId !== selectedProjectId
    ) {
      setPage(1);
      taskTypesAccRef.current = new Set();
      setStableTaskTypes([]);
      prevFiltersRef.current = { days, taskTypeFilter, blindSpotFilter, casePatternFilter, selectedProjectId };
    }
  }, [days, taskTypeFilter, blindSpotFilter, casePatternFilter, selectedProjectId, setPage]);

  const blindSpotFreq = useMemo(() => {
    return Object.entries(blindSpotCounts)
      .filter(([, count]) => count >= BLIND_SPOT_PILL_MIN_COUNT)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [blindSpotCounts]);
  const showBlindSpotPills = blindSpotFreq.length >= BLIND_SPOT_PILL_MIN_THEMES;
  const blindSpotPillsVisible = showBlindSpotPills ? blindSpotFreq.slice(0, BLIND_SPOT_PILL_CAP) : [];
  const blindSpotPillsHidden = showBlindSpotPills ? blindSpotFreq.length - blindSpotPillsVisible.length : 0;

  // edge_case_counts arrives pre-sorted from the backend; re-sort defensively
  // (count desc, then label asc) so badge order is stable regardless of source.
  const caseFreq = useMemo(
    () => Object.entries(edgeCaseCounts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
    [edgeCaseCounts]
  );

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const isStale = Date.now() - panelMountTimeRef.current > 300_000; // 5 minutes
  const handleRefresh = useCallback(() => {
    panelMountTimeRef.current = Date.now();
    fetchData();
  }, [fetchData]);

  if (!selectedProjectId) return null;

  return (
    <section aria-label="SR unhandled edges retrospectives" data-testid="sr-unhandled-edges-retrospectives-panel">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Unhandled Edge Cases</h2>

      <div className="flex flex-wrap items-center gap-2 mb-2 text-ui-sm">
        <RetrospectiveDayWindowButtons days={days} onChange={setDays} />
        {stableTaskTypes.length > 0 && (
          <select
            value={taskTypeFilter}
            onChange={e => setTaskTypeFilter(e.target.value)}
            className="bg-workbench-base border border-neutral-700 rounded px-1 py-0.5 text-neutral-300 text-ui-sm"
            aria-label="Filter by task type"
          >
            <option value="">All types</option>
            {stableTaskTypes.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
      </div>

      {showBlindSpotPills && (
        <div className="flex flex-wrap gap-1.5 mb-2" data-testid="sr-unhandled-edges-blind-spot-pills">
          {blindSpotPillsVisible.map(([theme, count]) => (
            // eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels
            <button
              key={theme}
              type="button"
              onClick={() => setBlindSpotFilter(theme === blindSpotFilter ? '' : theme)}
              className={`px-1.5 py-0.5 rounded text-ui-xs border transition-colors ${theme === blindSpotFilter ? 'border-amber-500/60 bg-amber-950/30 text-amber-300' : 'border-neutral-700 text-neutral-400 hover:border-amber-500/40'}`}
              aria-pressed={theme === blindSpotFilter}
              title={`Filter by blind-spot theme: ${theme}`}
            >
              {theme} <span className="text-neutral-500">{count}</span>
            </button>
          ))}
          {blindSpotPillsHidden > 0 && (
            <span className="text-ui-xs text-neutral-600 self-center ml-1">+{blindSpotPillsHidden} more</span>
          )}
        </div>
      )}

      {caseFreq.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2" data-testid="sr-unhandled-edges-case-pattern-badges">
          {caseFreq.map(([pattern, count]) => (
            // eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels
            <button
              key={pattern}
              type="button"
              onClick={() => setCasePatternFilter(pattern === casePatternFilter ? '' : pattern)}
              className={`px-1.5 py-0.5 rounded text-ui-xs border transition-colors ${pattern === casePatternFilter ? 'border-amber-500/60 bg-amber-950/30 text-amber-300' : 'border-neutral-700 text-neutral-400 hover:border-amber-500/40'}`}
              aria-pressed={pattern === casePatternFilter}
              title={`Filter by edge-case pattern: ${pattern}`}
            >
              {pattern} <span className="text-neutral-500">{count}</span>
            </button>
          ))}
        </div>
      )}

      {health && health.status !== 'healthy' && !healthDismissed && (
        <SrCatchRateHealthBanner health={health} onDismiss={() => setHealthDismissed(true)} />
      )}

      {loading ? (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 h-14 animate-pulse" />
      ) : fetchError ? (
        <div
          className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2"
          role="status"
        >
          <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
          <span>Error fetching SR data</span>
          <span className="text-ui-sm text-neutral-600 truncate" title={fetchError}>{fetchError}</span>
        </div>
      ) : items.length === 0 ? (
        (taskTypeFilter || blindSpotFilter || casePatternFilter) ? (
          <div
            className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2"
            role="status"
            data-testid="sr-unhandled-edges-empty"
          >
            <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
            <span>
              No unhandled edges
              {taskTypeFilter && <> for type &quot;{taskTypeFilter}&quot;</>}
              {blindSpotFilter && <> for theme &quot;{blindSpotFilter}&quot;</>}
              {casePatternFilter && <> matching &quot;{casePatternFilter}&quot;</>}
              .{' '}
              <button
                type="button"
                className="underline hover:text-neutral-300"
                onClick={() => { setTaskTypeFilter(''); setBlindSpotFilter(''); setCasePatternFilter(''); }}
              >Clear filter</button>
            </span>
          </div>
        ) : (
          <div
            className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-emerald-400 flex items-center gap-2"
            role="status"
            data-testid="sr-unhandled-edges-empty"
          >
            <CheckCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
            <span>No unhandled edges in the last {days} days</span>
          </div>
        )
      ) : (
        <>
          <div className="flex flex-col gap-1">
            {items.map(t => (
              <SrUnhandledEdgesRow key={t.task_id} task={t} onClick={() => onSelectTask(t.task_id)} />
            ))}
          </div>
          <RetrospectivePagination page={page} total={total} totalPages={totalPages} pageSize={PAGE_SIZE} setPage={setPage} />
          <RetrospectiveRefreshButton isStale={isStale} loading={loading} onRefresh={handleRefresh} />
        </>
      )}
    </section>
  );
}

function SrUnhandledEdgesRow({ task, onClick }: { task: SrUnhandledEdgesTask; onClick: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    // eslint-disable-next-line viewport/min-touch-target -- dense row, consistent with SrMissedRetrospective
    <div
      className="bg-workbench-base/50 border border-neutral-800/50 rounded px-2 py-1.5 text-ui-sm cursor-pointer hover:border-neutral-600"
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
        {task.scope && <span className="text-neutral-600 shrink-0">{task.scope}</span>}
        {task.unhandled_edges.length > 0 && (
          <span className="bg-amber-950/40 border border-amber-800/30 rounded px-1 py-0.5 text-amber-300 text-ui-xs shrink-0">
            {task.unhandled_edges.length} edge{task.unhandled_edges.length !== 1 ? 's' : ''}
          </span>
        )}
        {task.created_at && (
          <span className="text-neutral-600 shrink-0" title={task.created_at}>{timeAgo(task.created_at)}</span>
        )}
        {task.pr_url && isSafeUrl(task.pr_url) && (
          <a href={task.pr_url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="text-blue-400 hover:underline text-ui-xs shrink-0">PR</a>
        )}
      </div>
      {expanded && task.unhandled_edges.length > 0 && (
        <div className="mt-1.5 pl-4 flex flex-col gap-1">
          {task.unhandled_edges.map((edge: SrUnhandledEdge, i: number) => (
            <div key={i} className="rounded border border-neutral-800/60 bg-workbench-shell/50 p-1.5">
              <div className="mb-1 text-neutral-400 font-medium">{edge.case}</div>
              {edge.blind_spot && <div className="text-neutral-500 text-ui-xs">{edge.blind_spot}</div>}
              {edge.origin && <div className="text-neutral-600 text-ui-xs">source: {edge.origin}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
