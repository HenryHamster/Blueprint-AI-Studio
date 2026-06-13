import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import type { RefObject } from 'react';
import { Clock, ChevronDown, ChevronUp, Compass, CheckCircle, AlertCircle } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';
import { DirectionProgressSection } from './DirectionProgressSection';
import type { DirectionProgress } from './DirectionProgressSection';
import { ErrorBoundary } from './ErrorBoundary';
import { assertDigestContract, type DigestData as DigestDataBase } from '../types/digest';
import {
  loadDismissed,
  saveDismissed,
  getLastCatchUpViewed,
  windowToHours,
} from './catchupHelpers';
import type { TimeWindow, DismissedState } from './catchupHelpers';
import { sortDesc } from './catchupAnalytics';
import { SkeletonRow } from './catchupRows';
import { usePoller } from '../hooks/usePoller';
import { CatchUpReviewQualitySection } from './CatchUpReviewQualitySection';
import { RecentFailureGroupsSection } from './RecentFailureGroupsSection';
import { CatchUpNeedsAttentionSection } from './CatchUpNeedsAttentionSection';
import { CatchUpCostDiagnosticsSection } from './CatchUpCostDiagnosticsSection';
import { CatchUpRemediationPipelineSection } from './CatchUpRemediationPipelineSection';
import { CatchUpFailureSignalsSection } from './CatchUpFailureSignalsSection';
import { CatchUpInProgressSection } from './CatchUpInProgressSection';
import { CatchUpOperationalMetricsSection } from './CatchUpOperationalMetricsSection';
import { CatchUpStatsRow } from './CatchUpStatsRow';
import { CatchUpNarrativeSummary } from './CatchUpNarrativeSummary';
import { CatchUpLatestCycleSection } from './CatchUpLatestCycleSection';
import { CatchUpMemoPathwaySection } from './CatchUpMemoPathwaySection';
import { CatchUpResearchYieldSection } from './CatchUpResearchYieldSection';
import { CatchUpRecentCompletionsSection } from './CatchUpRecentCompletionsSection';
import type { Project } from '../types/project';

// DigestData (and FailureGroup / RecentCompletion / RecentFailure) are the
// canonical `/api/admin/digest` types, now hoisted to ../types/digest and shared
// with DashboardHome (single source of truth). DigestData is generic in the
// `direction_progress` element type; CatchUp feeds DirectionProgressSection's
// `DirectionProgress`, so it instantiates the canonical type with that.
type DigestData = DigestDataBase<DirectionProgress>;

async function fetchCatchUpLastSeen(projectId: string): Promise<number | null> {
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/last-seen?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const { last_seen_at: val } = await res.json() as { last_seen_at?: number | null };
    return typeof val === 'number' && val > 0 ? val : null;
  } catch { return null; }
}

async function postCatchUpLastSeen(projectId: string, timestamp: number): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/last-seen`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, timestamp }),
    });
  } catch { /* ignore — localStorage already saved */ }
}

export interface CatchUpPanelProps {
  selectedProjectId: string | null;
  projects?: Project[];
  lastVisitTimestamp: number | null;
  refreshInterval: number;
  onSelectTask: (task: { id: string }) => void;
  onOpenActivityFeed?: (filter?: string) => void;
  needsAttentionCount?: number;
  onApprove?: (taskId: string) => void;
  onRetry?: (taskId: string) => void;
  onBulkApprove?: (taskIds: string[]) => void;
  onBulkRetry?: (taskIds: string[]) => void;
  wsRef?: RefObject<WebSocket | null>;
  userEmail?: string | null;
}

export function CatchUpPanel({ selectedProjectId, projects, lastVisitTimestamp: _lastVisitTimestamp, refreshInterval, onSelectTask, onOpenActivityFeed, needsAttentionCount = 0, onApprove, onRetry, onBulkApprove, onBulkRetry, wsRef, userEmail }: CatchUpPanelProps) {
  // Per-operator localStorage key for catch-up viewed timestamp.
  // Falls back to the legacy global key when unauthenticated or email unknown.
  const catchUpViewedKey = userEmail
    ? `blueprint-admin:lastCatchUpViewed:${userEmail}`
    : 'blueprint-admin:lastCatchUpViewed';

  // Track when the user last viewed the catch-up panel (not page load).
  // Read once on mount; updated when the user collapses the panel after reading.
  // Per-operator key is preferred; falls back to global key (migration) then lastVisitTimestamp.
  const [lastCatchUpViewed, setLastCatchUpViewed] = useState<number | null>(() => {
    try {
      const stored = localStorage.getItem(catchUpViewedKey);
      const ts = stored ? parseInt(stored, 10) : null;
      if (ts && !isNaN(ts)) return ts;
      // Migration: seed from global key if per-operator key is absent
      if (userEmail) return getLastCatchUpViewed() ?? _lastVisitTimestamp;
      return _lastVisitTimestamp;
    } catch { return _lastVisitTimestamp; }
  });
  // Ref keeps backend-updated value accessible in fetchDigest without re-adding to its deps.
  const lastCatchUpViewedRef = useRef<number | null>(lastCatchUpViewed);
  lastCatchUpViewedRef.current = lastCatchUpViewed;
  const [collapsed, setCollapsed] = useState(() => {
    const wasCollapsed = localStorage.getItem('blueprint-admin:catchUpPanelCollapsed') === 'true';
    // Auto-expand on first session render if there are items needing attention
    if (wasCollapsed && needsAttentionCount > 0 && !sessionStorage.getItem('blueprint-admin:catchUpAutoExpanded')) {
      sessionStorage.setItem('blueprint-admin:catchUpAutoExpanded', 'true');
      return false;
    }
    return wasCollapsed;
  });
  const [activeWindow, setActiveWindow] = useState<TimeWindow>('last_visit');
  const [digest, setDigest] = useState<DigestData | null>(() => {
    if (!selectedProjectId) return null;
    try {
      const cached = localStorage.getItem(`blueprint-admin:lastDigest:${selectedProjectId}`);
      return cached ? JSON.parse(cached) : null;
    } catch {
      return null;
    }
  });
  const [loading, setLoading] = useState(false);
  const [updatingDigest, setUpdatingDigest] = useState(false);
  const [activeExpanded, setActiveExpanded] = useState(false);
  const [failuresExpanded, setFailuresExpanded] = useState(false);
  const [completionsExpanded, setCompletionsExpanded] = useState(false);
  const [expandedSnippets, setExpandedSnippets] = useState<Set<string>>(new Set());
  const [activeTaxonomyFilter, setActiveTaxonomyFilter] = useState<string | null>(null);
  const [activeStageFilter, setActiveStageFilter] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<DismissedState>(() => {
    const raw = loadDismissed();
    const now = Date.now();
    const state = Object.fromEntries(
      Object.entries(raw).filter(([, entry]) => !(entry.type === 'snooze' && entry.until <= now))
    ) as DismissedState;
    if (Object.keys(state).length < Object.keys(raw).length) saveDismissed(state);
    return state;
  });
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [showDismissed, setShowDismissed] = useState(false);
  const autoSectionExpandedRef = useRef(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  const selectedProject = projects?.find(p => p.id === selectedProjectId);
  const srCatchRateTargets = selectedProject?.sr_catch_rate_targets;

  const fetchDigest = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    else setUpdatingDigest(true);
    try {
      const hours = windowToHours(activeWindow, lastCatchUpViewedRef.current);
      const params = new URLSearchParams({ hours: String(hours) });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/digest?${params}`);
      if (res.ok) {
        const data: unknown = await res.json();
        // Validate against the shared contract before rendering/caching so a
        // backend rename or removal surfaces as "no data" rather than a silent
        // partial render (mirrors DashboardHome's guard — same digest payload).
        if (assertDigestContract<DirectionProgress>(data)) {
          setDigest(data);
          if (selectedProjectId) {
            try {
              localStorage.setItem(`blueprint-admin:lastDigest:${selectedProjectId}`, JSON.stringify(data));
            } catch { /* storage full */ }
          }
        }
      }
    } catch {
      // silently fail — digest is non-critical
    } finally {
      if (!silent) setLoading(false);
      else setUpdatingDigest(false);
    }
  }, [activeWindow, selectedProjectId]);

  // Load cached digest when project changes
  useEffect(() => {
    if (!selectedProjectId) { setDigest(null); return; }
    try {
      const cached = localStorage.getItem(`blueprint-admin:lastDigest:${selectedProjectId}`);
      setDigest(cached ? JSON.parse(cached) : null);
    } catch {
      setDigest(null);
    }
  }, [selectedProjectId]);

  // Sync last-seen from backend (same messages endpoint) — backend wins when newer (cross-device).
  useEffect(() => {
    if (!selectedProjectId) return;
    fetchCatchUpLastSeen(selectedProjectId).then(backendTs => {
      if (backendTs !== null && backendTs > (lastCatchUpViewedRef.current ?? 0)) {
        lastCatchUpViewedRef.current = backendTs;
        setLastCatchUpViewed(backendTs);
        try { localStorage.setItem(catchUpViewedKey, String(backendTs)); } catch { /* */ }
        fetchDigest(true);
      }
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId, catchUpViewedKey]);

  // Fetch on mount and when window/project changes; silent when collapsed
  useEffect(() => {
    if (collapsed) {
      fetchDigest(true);
    } else {
      fetchDigest();
    }
  }, [fetchDigest, collapsed]);

  // Auto-refresh on the shared, visibility-aware poll clock. Polling only
  // (the mount/window/project effect above owns the leading fetch); pauses
  // when the tab is hidden and catches up on return.
  usePoller(() => fetchDigest(true), {
    intervalMs: refreshInterval,
    enabled: !collapsed && refreshInterval !== 0,
    leading: false,
    deps: [fetchDigest, collapsed, refreshInterval],
  });

  // WebSocket-driven digest invalidation
  useEffect(() => {
    if (!wsRef) return;
    let attachedWs: WebSocket | null = null;

    const handleMessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.event === 'task_updated') {
          fetchDigest(true);
        }
      } catch {
        // ignore parse errors
      }
    };

    const attach = () => {
      const currentWs = wsRef.current;
      if (currentWs === attachedWs) return;
      if (attachedWs) attachedWs.removeEventListener('message', handleMessage);
      attachedWs = currentWs;
      if (currentWs) currentWs.addEventListener('message', handleMessage);
    };

    attach();
    const interval = setInterval(attach, 5000);
    return () => {
      clearInterval(interval);
      if (attachedWs) attachedWs.removeEventListener('message', handleMessage);
    };
  }, [wsRef, fetchDigest]);

  // Action wrappers: call prop then silently refresh digest after backend processes
  const handleApprove = useCallback((taskId: string) => {
    onApprove?.(taskId);
    setTimeout(() => fetchDigest(true), 1500);
  }, [onApprove, fetchDigest]);

  const handleRetry = useCallback((taskId: string) => {
    onRetry?.(taskId);
    setTimeout(() => fetchDigest(true), 1500);
  }, [onRetry, fetchDigest]);

  const handleBulkApprove = useCallback((taskIds: string[]) => {
    onBulkApprove?.(taskIds);
    setTimeout(() => fetchDigest(true), 1500);
  }, [onBulkApprove, fetchDigest]);

  const handleBulkRetry = useCallback((taskIds: string[]) => {
    onBulkRetry?.(taskIds);
    setTimeout(() => fetchDigest(true), 1500);
  }, [onBulkRetry, fetchDigest]);

  // Auto-expand most relevant section on initial digest load
  useEffect(() => {
    if (!digest || autoSectionExpandedRef.current) return;
    autoSectionExpandedRef.current = true;
    const attentionIdSet = new Set((digest.needs_attention ?? []).map(i => i.task_id));
    const extra = (digest.recent_failures ?? []).filter(f => !attentionIdSet.has(f.task_id));
    if ((digest.needs_attention ?? []).length > 0) {
      // needs_attention is always visible — nothing to expand
    } else if ((digest.recent_completions ?? []).length > 0) {
      setCompletionsExpanded(true);
    } else if (extra.length > 0) {
      setFailuresExpanded(true);
    } else if (digest.active_count > 0) {
      setActiveExpanded(true);
    }
  }, [digest]);

  // Close dropdown on outside click
  useEffect(() => {
    if (!openDropdown) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpenDropdown(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [openDropdown]);

  const dismissItem = (taskId: string) => {
    const next = { ...dismissed, [taskId]: { type: 'dismiss' as const } };
    setDismissed(next);
    saveDismissed(next);
    setOpenDropdown(null);
  };

  const snoozeItem = (taskId: string, hours: number) => {
    const until = Date.now() + hours * 3600 * 1000;
    const next = { ...dismissed, [taskId]: { type: 'snooze' as const, until } };
    setDismissed(next);
    saveDismissed(next);
    setOpenDropdown(null);
  };

  const restoreItem = useCallback((taskId: string) => {
    const next = Object.fromEntries(
      Object.entries(dismissed).filter(([k]) => k !== taskId)
    ) as DismissedState;
    setDismissed(next);
    saveDismissed(next);
  }, [dismissed]);

  const isHidden = useCallback((taskId: string): boolean => {
    const entry = dismissed[taskId];
    if (!entry) return false;
    if (entry.type === 'dismiss') return true;
    if (entry.type === 'snooze') return Date.now() < entry.until;
    return false;
  }, [dismissed]);

  const visibleItems = useMemo(
    () => (digest?.needs_attention ?? []).filter(item => !isHidden(item.task_id)),
    [digest?.needs_attention, isHidden]
  );
  const approvalItems = useMemo(
    () => visibleItems.filter(i => i.reason === 'approval_required'),
    [visibleItems]
  );
  const failedItems = useMemo(
    () => visibleItems.filter(i => i.reason === 'failed'),
    [visibleItems]
  );

  const handleBulkApproveAll = useCallback(() => {
    handleBulkApprove(approvalItems.map(i => i.task_id));
  }, [handleBulkApprove, approvalItems]);

  const handleBulkRetryAll = useCallback(() => {
    handleBulkRetry(failedItems.map(i => i.task_id));
  }, [handleBulkRetry, failedItems]);

  const toggle = () => {
    const next = !collapsed;
    if (next) {
      // Collapsing → user has read the digest, record per-operator timestamp locally and cross-device
      const now = Date.now();
      lastCatchUpViewedRef.current = now;
      try { localStorage.setItem(catchUpViewedKey, String(now)); } catch { /* */ }
      if (selectedProjectId) void postCatchUpLastSeen(selectedProjectId, now);
    }
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:catchUpPanelCollapsed', String(next));
  };

  const toggleSnippet = useCallback((id: string) => {
    setExpandedSnippets(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleToggleShowDismissed = useCallback(() => setShowDismissed(v => !v), []);
  const handleToggleActiveExpanded = useCallback(() => setActiveExpanded(v => !v), []);
  const handleToggleCompletionsExpanded = useCallback(() => setCompletionsExpanded(v => !v), []);
  const handleToggleFailuresExpanded = useCallback(() => setFailuresExpanded(v => !v), []);

  const handleWindowChange = useCallback((e: React.MouseEvent<HTMLButtonElement>) => {
    setActiveWindow(e.currentTarget.dataset.key as TimeWindow);
  }, []);

  // Aggregate "needs attention" count from the digest, which covers the whole
  // project/fleet window — not just the current paginated task page. Prefer the
  // explicit needs_attention list; fall back to pending-approval + failed counts
  // when an older payload omits it. `null` until the digest has loaded, at which
  // point the page-derived `needsAttentionCount` prop is used as the fallback.
  const digestAttentionCount = digest
    ? ((digest.needs_attention?.length) ?? (digest.pending_approval_count + digest.failed_count))
    : null;
  const attentionCount = digestAttentionCount ?? needsAttentionCount;

  // First-render auto-expand, driven by the aggregate digest count so a
  // paginated page that hides other projects' attention items can't suppress the
  // expand. Runs once per session (shared sessionStorage guard with the
  // synchronous prop-based fallback in the `collapsed` initializer above), and
  // only once the digest has loaded.
  const didDigestAutoExpandRef = useRef(false);
  useEffect(() => {
    if (didDigestAutoExpandRef.current) return;
    if (digestAttentionCount == null) return; // wait for the digest to load
    didDigestAutoExpandRef.current = true;
    if (sessionStorage.getItem('blueprint-admin:catchUpAutoExpanded')) return;
    if (
      digestAttentionCount > 0 &&
      localStorage.getItem('blueprint-admin:catchUpPanelCollapsed') === 'true'
    ) {
      sessionStorage.setItem('blueprint-admin:catchUpAutoExpanded', 'true');
      setCollapsed(false);
    }
  }, [digestAttentionCount]);

  const hasActivity = digest && (
    digest.completed_count > 0 ||
    digest.failed_count > 0 ||
    digest.pending_approval_count > 0 ||
    digest.research_cycles > 0 ||
    digest.prs_opened > 0 ||
    digest.active_count > 0
  );

  // Failures not already in needs_attention
  const attentionIds = useMemo(() => new Set((digest?.needs_attention ?? []).map(i => i.task_id)), [digest?.needs_attention]);
  const extraFailureGroups = useMemo(() => (digest?.failure_groups ?? []).map(g => ({ ...g, tasks: g.tasks.filter(t => !attentionIds.has(t.task_id)) })).filter(g => g.tasks.length > 0), [digest?.failure_groups, attentionIds]);

  const windowLabels: { key: TimeWindow; label: string }[] = [
    { key: 'last_visit', label: lastCatchUpViewed ? 'Since last visit' : 'Since last visit (24h)' },
    { key: '6h', label: 'Last 6h' },
    { key: '24h', label: 'Last 24h' },
    { key: '7d', label: 'Last 7d' },
  ];

  const { frbcEntries, mrbcEntries, cmbcEntries, carTypeEntries, dropOffEntries } = useMemo(() => {
    const rf = digest?.remediation_funnel;
    const frbc = rf?.failed_remediations_by_class ?? {};
    const mrbc = rf?.merged_remediations_by_trigger_class ?? {};
    const cmbc = rf?.cumulative_merged_by_trigger_class ?? {};
    const car = rf?.ci_auto_remediation;
    const dropOff = rf?.diagnosis_drop_off_by_reason ?? {};
    return {
      frbcEntries: sortDesc(Object.entries(frbc).filter(([, v]) => v > 0), v => v),
      mrbcEntries: sortDesc(Object.entries(mrbc).filter(([, v]) => v > 0), v => v),
      cmbcEntries: sortDesc(Object.entries(cmbc).filter(([, v]) => v > 0), v => v),
      carTypeEntries: sortDesc(Object.entries(car?.by_ci_failure_type ?? {}).filter(([, v]) => (v.spawned + v.merged + v.failed) > 0), v => v.spawned),
      dropOffEntries: sortDesc(Object.entries(dropOff).filter(([, v]) => v > 0), v => v),
    };
  }, [digest?.remediation_funnel]);

  return (
    <div className="mb-4 shrink-0">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-2">
        <button
          onClick={toggle}
          aria-expanded={!collapsed}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Clock className="w-3 h-3" />
          Catch-up
          {collapsed && attentionCount > 0 && (
            <span className="ml-1 bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
              {attentionCount} need attention
            </span>
          )}
          {collapsed && digest && (
            <span className="text-[10px] text-neutral-500 font-mono ml-1 normal-case tracking-normal font-normal">
              {digest.completed_count} shipped
              {digest.failed_count > 0 && (
                <span className="text-red-400"> · {digest.failed_count}F</span>
              )}
              {digest.pending_approval_count > 0 && (
                <span className="text-amber-400"> · {digest.pending_approval_count}A</span>
              )}
              {' · '}{digest.success_rate != null ? `${Math.round(digest.success_rate * 100)}%` : '—'}
              {digest.trend && (
                <>
                  {' · '}
                  <span className={
                    digest.trend === 'improving' ? 'text-emerald-400' :
                    digest.trend === 'declining' ? 'text-red-400' :
                    'text-neutral-500'
                  }>{digest.trend}</span>
                </>
              )}
            </span>
          )}
          {updatingDigest && (
            <span className="text-[9px] text-neutral-600 font-normal normal-case tracking-normal ml-1">Updating...</span>
          )}
        </button>
        {!collapsed && (
          <div className="flex flex-wrap items-center gap-1 sm:ml-2">
            {windowLabels.map(({ key, label }) => (
              <button
                key={key}
                onClick={handleWindowChange}
                data-key={key}
                aria-pressed={activeWindow === key}
                className={`px-2 min-h-[44px] flex items-center rounded-full text-[10px] font-medium transition-colors ${
                  activeWindow === key
                    ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                    : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
                }`}
              >
                {label}
              </button>
            ))}
            {onOpenActivityFeed && (
              <button
                onClick={() => onOpenActivityFeed()}
                className="px-2 min-h-[44px] flex items-center gap-1 rounded-full text-[10px] font-medium text-neutral-500 hover:text-neutral-300 border border-transparent transition-colors sm:ml-auto"
                title="Open chronological activity timeline"
              >
                <Compass className="w-3 h-3" />
                Activity
              </button>
            )}
          </div>
        )}
      </div>

      {collapsed && digest?.narrative_summary && (
        <div className="text-[10px] text-neutral-500 mt-0.5 pl-7 truncate max-w-[calc(100vw-2rem)] sm:max-w-xl">
          {digest.narrative_summary.slice(0, 100)}{digest.narrative_summary.length > 100 ? '…' : ''}
        </div>
      )}

      {!collapsed && (
        <div className="mt-2 bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-3 sm:p-4">
          {loading ? (
            <div className="flex flex-col gap-2">
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
            </div>
          ) : !digest ? (
            <div className="flex items-center gap-2 text-xs text-neutral-500 py-1">
              <AlertCircle className="w-3.5 h-3.5" />
              Catch-up data unavailable — retry
            </div>
          ) : !hasActivity ? (
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-2 text-xs text-neutral-600 py-1">
                <CheckCircle className="w-3.5 h-3.5" />
                All clear — nothing to catch up on
              </div>
              {digest.served_stale && (
                <div className="flex items-center gap-1 text-[10px] text-amber-500/80">
                  <AlertCircle className="w-3 h-3" />
                  Stale data — last fetch may be outdated
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {digest.served_stale && (
                <div className="flex items-center gap-1 text-[10px] text-amber-500/80">
                  <AlertCircle className="w-3 h-3" />
                  Stale data — last fetch may be outdated
                </div>
              )}
              <CatchUpNarrativeSummary digest={digest} activeWindow={activeWindow} />
              {digest.latest_cycle && (
                <CatchUpLatestCycleSection latestCycle={digest.latest_cycle} />
              )}
              <CatchUpMemoPathwaySection digest={digest} />
              <CatchUpStatsRow digest={digest} />
              <CatchUpResearchYieldSection pipelineFilterFunnel={digest.pipeline_filter_funnel} />

              {/* Direction progress */}
              {digest.direction_progress && digest.direction_progress.length > 0 && (
                <ErrorBoundary level="widget">
                  <DirectionProgressSection directions={digest.direction_progress} medianCostPerMergedPr={digest.median_cost_per_merged_pr} />
                </ErrorBoundary>
              )}

              {/* Needs attention */}
              <CatchUpNeedsAttentionSection
                items={digest.needs_attention ?? []}
                visibleItems={visibleItems}
                approvalItems={approvalItems}
                failedItems={failedItems}
                dismissed={dismissed}
                expandedSnippets={expandedSnippets}
                openDropdown={openDropdown}
                showDismissed={showDismissed}
                dropdownRef={dropdownRef}
                isHidden={isHidden}
                onSelectTask={onSelectTask}
                onApprove={onApprove}
                onRetry={onRetry}
                onBulkApprove={onBulkApprove}
                onBulkRetry={onBulkRetry}
                handleApprove={handleApprove}
                handleRetry={handleRetry}
                handleBulkApproveAll={handleBulkApproveAll}
                handleBulkRetryAll={handleBulkRetryAll}
                setOpenDropdown={setOpenDropdown}
                toggleSnippet={toggleSnippet}
                dismissItem={dismissItem}
                snoozeItem={snoozeItem}
                restoreItem={restoreItem}
                handleToggleShowDismissed={handleToggleShowDismissed}
              />

              {/* In progress */}
              <CatchUpInProgressSection
                activeCount={digest.active_count}
                activeTasks={digest.active_tasks ?? []}
                expanded={activeExpanded}
                onToggle={handleToggleActiveExpanded}
                onSelectTask={onSelectTask}
              />

              <CatchUpCostDiagnosticsSection
                failureTaxonomyDistribution={digest.failure_taxonomy_distribution}
                costByFailureClass={digest.cost_by_failure_class}
                failureGroups={digest.failure_groups}
                failureStageDistribution={digest.failure_stage_distribution}
                scopeConversionRates={digest.scope_conversion_rates}
                prevScopeConversionRates={digest.prev_scope_conversion_rates}
                scopeMismatchSummary={digest.scope_mismatch_summary}
                rejectionPatternSummary={digest.rejection_pattern_summary}
                costPerMergeByType={digest.cost_per_merge_by_type}
                medianCostPerMergeByType={digest.median_cost_per_merge_by_type}
                trendHistory={digest.trend_history}
                activeTaxonomyFilter={activeTaxonomyFilter}
                activeStageFilter={activeStageFilter}
                onTaxonomyFilterChange={setActiveTaxonomyFilter}
                onStageFilterChange={setActiveStageFilter}
                onExpandFailures={() => setFailuresExpanded(true)}
              />

              <CatchUpFailureSignalsSection
                recurringErrorClusters={digest.recurring_error_clusters ?? []}
                earlyWarnings={digest.early_warnings ?? []}
                recurringPatterns={digest.recurring_patterns ?? []}
                directionProgress={digest.direction_progress ?? []}
                onSelectTask={onSelectTask}
              />

              <CatchUpOperationalMetricsSection
                directionProgress={digest.direction_progress ?? []}
                reviewCapExhaustionSummary={digest.review_cap_exhaustion_summary}
                totalMergeRetriesSaved={digest.total_merge_retries_saved}
                mergeEarlyExitCount={digest.merge_early_exit_count}
                retryTaskCount={digest.retry_task_count}
                retryOverheadCostUsd={digest.retry_overhead_cost_usd}
                avgRetriesBeforeFailure={digest.avg_retries_before_failure}
                retryWasteByDirection={digest.retry_waste_by_direction}
                retrySuccessCount={digest.retry_success_count}
                retryWastedRate={digest.retry_wasted_rate}
                retryRecoveryCostUsd={digest.retry_recovery_cost_usd}
                resurrectionOutcomeSummary={digest.resurrection_outcome_summary}
                migrationHealth={digest.migration_health}
                migrationSlotContention={digest.migration_slot_contention}
                preventionEfficacy={digest.prevention_efficacy}
                releaseQualifierInfraMetrics={digest.release_qualifier_infra_metrics}
                retryPatternSummary={digest.retry_pattern_summary}
                analysisOverflowSummary={digest.analysis_overflow_summary}
                provisionWasteCostUsd={digest.provision_waste_cost_usd}
                provisionWasteByDirection={digest.provision_waste_by_direction}
                provisioningOverheadPct={digest.provisioning_overhead_pct}
                escalationOutcomeSummary={digest.escalation_outcome_summary}
                retrySuccessRateByClass={digest.retry_success_rate_by_class}
              />

              {/* Review quality */}
              <CatchUpReviewQualitySection
                selfReviewEffectiveness={digest.self_review_effectiveness}
                selfReviewPredictionAccuracy={digest.self_review_prediction_accuracy}
                selfReviewPredictionAccuracyTrends={digest.self_review_prediction_accuracy_trends}
                selfReviewSkipRate={digest.self_review_skip_rate}
                reviewAccuracy={digest.review_accuracy}
                firstReviewApprovalRate={digest.first_review_approval_rate}
                freshReviewerEffectiveness={digest.fresh_reviewer_effectiveness}
                srCatchRateTargets={srCatchRateTargets}
              />


              {/* Recent failures grouped by classification (not in needs_attention) */}
              <RecentFailureGroupsSection
                extraFailureGroups={extraFailureGroups}
                activeTaxonomyFilter={activeTaxonomyFilter}
                activeStageFilter={activeStageFilter}
                trendHistory={digest.trend_history}
                costByFailureClass={digest.cost_by_failure_class}
                expandedSnippets={expandedSnippets}
                onToggleSnippet={toggleSnippet}
                onSelectTask={onSelectTask}
                failuresExpanded={failuresExpanded}
                onToggleFailuresExpanded={handleToggleFailuresExpanded}
              />

              {/* Remediation pipeline */}
              {digest.remediation_funnel && (
                digest.remediation_funnel.investigations_spawned > 0
                || (digest.remediation_funnel.ci_auto_remediation?.spawned ?? 0) > 0
                || Object.keys(digest.remediation_funnel.cumulative_merged_by_trigger_class ?? {}).length > 0
              ) && (
                <CatchUpRemediationPipelineSection
                  remediationFunnel={digest.remediation_funnel}
                  frbcEntries={frbcEntries}
                  mrbcEntries={mrbcEntries}
                  cmbcEntries={cmbcEntries}
                  carTypeEntries={carTypeEntries}
                  dropOffEntries={dropOffEntries}
                />
              )}

              {/* Recent completions */}
              <CatchUpRecentCompletionsSection
                completions={digest.recent_completions ?? []}
                expanded={completionsExpanded}
                onToggle={handleToggleCompletionsExpanded}
                onSelectTask={onSelectTask}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
