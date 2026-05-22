import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import type { RefObject } from 'react';
import { Clock, ChevronDown, ChevronUp, AlertTriangle, CheckCircle, XCircle, Loader2, Check, RotateCcw, DollarSign, Compass } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo, parseTimestamp } from '../utils/time';
import { isSafeUrl, TaskTypeBadge, TASK_TYPE_STYLES, FAILURE_CLASS_CONFIG } from './TaskCard';
import type { TaskType } from './TaskCard';
import { DirectionProgressSection } from './DirectionProgressSection';
import type { DirectionProgress } from './DirectionProgressSection';
import { ErrorBoundary } from './ErrorBoundary';
import { TAXONOMY_ORDER, TAXONOMY_LABELS, TAXONOMY_COLORS, STAGE_ORDER, STAGE_COLORS } from './taxonomyConstants';
import {
  TIMEOUT_SUBTYPE_LABELS,
  loadDismissed,
  saveDismissed,
  formatSnoozeRemaining,
  getLastCatchUpViewed,
  markCatchUpViewed,
  windowToHours,
} from './catchupHelpers';
import type { RecentCompletion, TimeWindow, DismissedState } from './catchupHelpers';
import { WaitingBadge, CompletionRow, SkeletonRow } from './catchupRows';
import { SrTrendBreakdown, type SrTrendData } from './SrTrendBreakdown';
import { PipelineRegressionsPanel } from './DashboardHome';
import type { Project } from '../types/project';

interface NeedsAttentionItem {
  task_id: string;
  title: string;
  reason: 'failed' | 'approval_required';
  error_snippet: string | null;
  pr_url: string | null;
  waiting_since?: string;
  waiting_hours?: number;
  approval_reason?: string;
  touched_critical?: string[];
  failure_class?: string | null;
  scope?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
  merge_retries_saved?: number | null;
}

interface RecentFailure {
  task_id: string;
  title: string;
  failed_at: string | null;
  error_snippet: string | null;
  waiting_hours?: number;
  attempt_count?: number;
  timeout_subtype?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
  task_type?: string;
  failure_class?: string | null;
  scope?: string | null;
}

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

interface ActiveTask {
  task_id: string;
  title: string;
  status: string;
  task_type: string;
  started_at: string | null;
  running_cost_usd?: number | null;
  direction_text?: string | null;
  scope?: string | null;
  review_iteration?: number | null;
  review_cap?: number | null;
}

interface DigestData {
  window_hours: number;
  generated_at: string;
  completed_count: number;
  failed_count: number;
  pending_approval_count: number;
  research_cycles: number;
  prs_opened: number;
  active_count: number;
  running_count: number;
  queued_count: number;
  active_tasks: ActiveTask[];
  needs_attention: NeedsAttentionItem[];
  recent_failures: RecentFailure[];
  failure_groups?: FailureGroup[];
  recent_completions: RecentCompletion[];
  success_rate?: number | null;
  narrative_summary?: string;
  direction_progress?: DirectionProgress[];
  prev_completed_count?: number;
  prev_failed_count?: number;
  prev_success_rate?: number | null;
  completed_delta?: number;
  failed_delta?: number;
  success_rate_delta?: number | null;
  trend?: 'improving' | 'declining' | 'stable';
  trend_history?: {period_end: string; success_rate: number | null; completed: number; failed: number; failed_by_taxonomy?: Record<string, number>; failed_by_class?: Record<string, number>; first_attempt_success_rate?: number | null; period_cost_usd?: number | null}[];
  failure_taxonomy_distribution?: {
    categories: Record<string, { count: number; percentage: number }>;
    total_failures: number;
    dominant_category: string | null;
  } | null;
  scope_mismatch_summary?: {
    total_calibrated: number;
    calibrated_success_rate: number | null;
    by_original_scope: Record<string, { total: number; completed: number; failed: number }>;
    top_directions: {
      direction_id: string;
      direction_text: string;
      calibrated_count: number;
      total_tasks: number;
      calibration_rate: number;
    }[];
  } | null;
  failure_stage_distribution?: {
    stages: Record<string, number>;
    total_classified: number;
    dominant_stage: string | null;
  } | null;
  cost_by_failure_class?: Record<string, number> | null;
  scope_conversion_rates?: Record<string, { created: number; merged: number; calibrated_in?: number; calibrated_out?: number }> | null;
  prev_scope_conversion_rates?: Record<string, { created: number; merged: number; calibrated_in?: number; calibrated_out?: number }> | null;
  total_failed_cost_usd?: number | null;
  retry_overhead_cost_usd?: number | null;
  retry_waste_by_direction?: { direction_id: string; direction_text: string; retry_cost_usd: number; retry_task_count: number }[] | null;
  retry_task_count?: number | null;
  avg_retries_before_failure?: number | null;
  retry_success_count?: number | null;
  retry_wasted_rate?: number | null;
  retry_recovery_cost_usd?: number | null;
  resurrection_outcome_summary?: {
    total: number;
    completed: number;
    failed_again: number;
    success_rate: number | null;
  } | null;
  total_merge_retries_saved?: number | null;
  merge_early_exit_count?: number | null;
  total_estimated_cost_usd?: number | null;
  spot_cost_usd?: number | null;
  on_demand_cost_usd?: number | null;
  provisioning_overhead_usd?: number | null;
  daily_cost_usd?: number | null;
  daily_cost_ceiling_usd?: number | null;
  rejection_pattern_summary?: {
    total_rejected: number;
    top_notes: { snippet: string; count: number }[];
  } | null;
  retry_pattern_summary?: {
    total_multi_attempt_failures: number;
    repeat_class_count: number;
    top_patterns: { sequence: string; count: number }[];
  } | null;
  recurring_error_clusters?: {
    fingerprint: string;
    count: number;
    error_snippet: string;
    direction_ids: string[];
    task_types: string[];
    first_task_id: string;
  }[] | null;
  early_warnings?: {
    failure_class: string;
    rate_progression: number[];
    affected_direction_count: number;
    suggested_action?: string | null;
    severity?: string | null;
  }[] | null;
  recurring_patterns?: {
    fingerprint: string;
    occurrence_count: number;
    first_seen?: string | null;
    last_seen?: string | null;
    severity?: string | null;
    example_message?: string | null;
    affected_direction_count: number;
    remediation_status: 'fix_verified' | 'fix_shipped' | 'investigating' | 'unaddressed';
    proactive?: boolean;
  }[] | null;
  review_cap_exhaustion_summary?: {
    count: number;
    avg_iterations: number | null;
    total_cost_usd: number;
    avg_cost_usd: number | null;
    top_directions: { direction_id: string; count: number }[];
  } | null;
  analysis_overflow_summary?: {
    total_count: number;
    by_scope: Record<string, number>;
    cost_usd: number;
  } | null;
  provision_waste_cost_usd?: number | null;
  provision_waste_by_direction?: {
    direction_id: string;
    direction_text: string;
    provision_cost_usd: number;
    provision_task_count: number;
  }[] | null;
  provisioning_overhead_pct?: number | null;
  retry_success_rate_by_class?: Record<string, { merged: number; failed: number; success_rate: number }> | null;
  escalation_outcome_summary?: {
    total_attempted: number;
    total_merged: number;
    total_failed: number;
    overall_success_rate: number | null;
    by_path: Record<string, { attempted: number; merged: number; failed: number; success_rate: number | null }>;
  } | null;
  self_review_effectiveness?: {
    with_self_review_approval_rate: number | null;
    without_self_review_approval_rate: number | null;
    clean_verdict_approval_rate: number | null;
    fixed_verdict_approval_rate: number | null;
    sample_size: number;
  } | null;
  self_review_prediction_accuracy?: {
    prediction_hit_rate: number | null;
    miss_rate: number | null;
    false_alarm_rate: number | null;
    sample_size: number;
  } | null;
  self_review_prediction_accuracy_trends?: SrTrendData | null;
  self_review_skip_rate?: {
    overall_rate: number;
    skipped_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; skipped_count: number; sample_size: number }>;
  } | null;
  first_review_approval_rate?: {
    overall: number | null;
    sample_size: number;
    by_type?: Record<string, number> | null;
    by_scope?: Record<string, number> | null;
    with_playbook?: number | null;
    without_playbook?: number | null;
    with_measurements?: number | null;
    without_measurements?: number | null;
  } | null;
  review_accuracy?: {
    total_reviews: number;
    overrides: number;
    dismissals: number;
    misses: number;
    total_approvals: number;
    precision: number | null;
    recall: number | null;
  } | null;
  first_attempt_success_rate?: {
    overall: number | null;
    with_playbook: number | null;
    without_playbook: number | null;
    with_arch_context: number | null;
    without_arch_context: number | null;
    sample_size: number;
    first_attempt_by_review_category?: Record<string, number> | null;
    trend?: string | null;
    by_type?: Record<string, number> | null;
    by_scope?: Record<string, number> | null;
  } | null;
  pipeline_acceptance_rate?: number | null;
  pipeline_conversion_rate?: number | null;
  overall_product_ratio?: number | null;
  overall_type_distribution?: Record<string, number> | null;
  pipeline_filter_funnel?: Record<string, number> | null;
  latest_cycle?: {
    cycle_id?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
    outcome?: string | null;
    directions_created?: number | null;
    directions_retired?: number | null;
    vision_updated?: boolean | null;
    summary_text?: string | null;
  } | null;
  remediation_funnel?: {
    investigations_spawned: number;
    investigations_completed: number;
    investigations_with_diagnosis: number;
    remediations_spawned: number;
    remediations_merged: number;
    remediations_failed: number;
    fixes_held?: number;
    avg_time_to_remediation_hours: number | null;
    active_investigations: number;
    investigations_by_confidence?: Record<string, number>;
    failed_remediations_by_class?: Record<string, number>;
    merged_remediations_by_trigger_class?: Record<string, number>;
    cumulative_merged_by_trigger_class?: Record<string, number>;
    ci_auto_remediation?: {
      spawned: number;
      merged: number;
      failed: number;
      by_ci_failure_type?: Record<string, { spawned: number; merged: number; failed: number }>;
    };
    remediation_success_by_confidence?: Record<string, { merged: number; failed: number }>;
    recent_investigations?: Array<{
      task_id: string;
      direction_id?: string | null;
      summary?: string | null;
      confidence?: string | null;
      severity?: string | null;
      completed_at?: string | null;
    }>;
    active_investigations_detail?: Array<{
      task_id: string;
      direction_id?: string | null;
      elapsed_hours?: number | null;
    }>;
    diagnosis_drop_off_by_reason?: Record<string, number>;
  } | null;
  median_cost_per_merged_pr?: number | null;
  fresh_reviewer_effectiveness?: {
    fresh_reviewer_sample_size: number;
    fresh_reviewer_first_attempt_approval_rate: number | null;
    baseline_first_attempt_approval_rate: number | null;
    per_category_hit_rate: Record<string, number>;
    reviewer_finding_added_value: Record<string, number>;
    prediction_sample_size: number;
  } | null;
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
}

export function CatchUpPanel({ selectedProjectId, projects, lastVisitTimestamp: _lastVisitTimestamp, refreshInterval, onSelectTask, needsAttentionCount = 0, onApprove, onRetry, onBulkApprove, onBulkRetry, wsRef }: CatchUpPanelProps) {
  // Track when the user last viewed the catch-up panel (not page load).
  // Read once on mount; updated when the user collapses the panel after reading.
  const [lastCatchUpViewed] = useState(() => getLastCatchUpViewed() ?? _lastVisitTimestamp);
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
  const [remediationExpanded, setRemediationExpanded] = useState(false);
  const [investigationsExpanded, setInvestigationsExpanded] = useState(false);
  const [cycleSummaryExpanded, setCycleSummaryExpanded] = useState(false);
  const [reviewQualityExpanded, setReviewQualityExpanded] = useState(false);
  const [expandedSnippets, setExpandedSnippets] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
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
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const autoSectionExpandedRef = useRef(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  const selectedProject = projects?.find(p => p.id === selectedProjectId);
  const srCatchRateTargets = selectedProject?.sr_catch_rate_targets;

  const fetchDigest = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    else setUpdatingDigest(true);
    try {
      const hours = windowToHours(activeWindow, lastCatchUpViewed);
      const params = new URLSearchParams({ hours: String(hours) });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/digest?${params}`);
      if (res.ok) {
        const data: DigestData = await res.json();
        setDigest(data);
        if (selectedProjectId) {
          try {
            localStorage.setItem(`blueprint-admin:lastDigest:${selectedProjectId}`, JSON.stringify(data));
          } catch { /* storage full */ }
        }
      }
    } catch {
      // silently fail — digest is non-critical
    } finally {
      if (!silent) setLoading(false);
      else setUpdatingDigest(false);
    }
  }, [activeWindow, lastCatchUpViewed, selectedProjectId]);

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

  // Fetch on mount and when window/project changes; silent when collapsed
  useEffect(() => {
    if (collapsed) {
      fetchDigest(true);
    } else {
      fetchDigest();
    }
  }, [fetchDigest, collapsed]);

  // Auto-refresh
  useEffect(() => {
    if (collapsed || refreshInterval === 0) return;
    intervalRef.current = setInterval(() => fetchDigest(true), refreshInterval);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [fetchDigest, collapsed, refreshInterval]);

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

  const restoreItem = (taskId: string) => {
    const next = Object.fromEntries(
      Object.entries(dismissed).filter(([k]) => k !== taskId)
    ) as DismissedState;
    setDismissed(next);
    saveDismissed(next);
  };

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

  const toggle = () => {
    const next = !collapsed;
    if (next) {
      // Collapsing → user has read the digest, record the timestamp
      markCatchUpViewed();
    }
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:catchUpPanelCollapsed', String(next));
  };

  const toggleSnippet = (id: string) => {
    setExpandedSnippets(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const hasActivity = digest && (
    digest.completed_count > 0 ||
    digest.failed_count > 0 ||
    digest.pending_approval_count > 0 ||
    digest.research_cycles > 0 ||
    digest.prs_opened > 0 ||
    digest.active_count > 0
  );

  // Failures not already in needs_attention
  const attentionIds = new Set((digest?.needs_attention ?? []).map(i => i.task_id));
  const extraFailureGroups = (digest?.failure_groups ?? [])
    .map(g => ({ ...g, tasks: g.tasks.filter(t => !attentionIds.has(t.task_id)) }))
    .filter(g => g.tasks.length > 0);

  const windowLabels: { key: TimeWindow; label: string }[] = [
    { key: 'last_visit', label: lastCatchUpViewed ? 'Since last visit' : 'Since last visit (24h)' },
    { key: '6h', label: 'Last 6h' },
    { key: '24h', label: 'Last 24h' },
    { key: '7d', label: 'Last 7d' },
  ];

  const costs = useMemo(
    () => (digest?.trend_history ?? []).filter(p => p.period_cost_usd != null).map(p => p.period_cost_usd as number),
    [digest?.trend_history]
  );

  const directionCostTotals = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const dp of digest?.direction_progress ?? []) {
      if (!dp.cost_by_scope) continue;
      for (const [scope, usd] of Object.entries(dp.cost_by_scope)) {
        if (typeof usd === 'number' && usd > 0) {
          totals[scope] = (totals[scope] ?? 0) + usd;
        }
      }
    }
    return totals;
  }, [digest?.direction_progress]);

  const FILTER_KEYS: { key: string; label: string }[] = [
    { key: 'hash_dedup', label: 'hash dedup' },
    { key: 'word_overlap', label: 'word overlap' },
    { key: 'file_overlap', label: 'file overlap' },
    { key: 'type_filtered', label: 'type filter' },
    { key: 'empty_prompt', label: 'empty prompt' },
    { key: 'batch_cap', label: 'batch cap' },
  ];

  const drops = useMemo(() => {
    const ff = digest?.pipeline_filter_funnel;
    if (!ff) return [];
    return FILTER_KEYS
      .map(f => ({ ...f, v: ff[f.key] ?? 0 }))
      .filter(f => f.v > 0)
      .sort((a, b) => b.v - a.v);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [digest?.pipeline_filter_funnel]);

  const history = useMemo(() => digest?.trend_history ?? [], [digest?.trend_history]);

  const trendFor = useCallback((cat: string): { arrow: string; color: string } => {
    if (history.length < 4) return { arrow: '–', color: 'text-neutral-500' };
    const sumAt = (arr: typeof history) => arr.reduce((a, p) => a + (p.failed_by_taxonomy?.[cat] ?? 0), 0);
    const recent = sumAt(history.slice(-2));
    const prior = sumAt(history.slice(-4, -2));
    if (recent > prior) return { arrow: '↑', color: 'text-red-400' };
    if (recent < prior) return { arrow: '↓', color: 'text-emerald-400' };
    return { arrow: '–', color: 'text-neutral-500' };
  }, [history]);

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
          {collapsed && needsAttentionCount > 0 && (
            <span className="ml-1 bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
              {needsAttentionCount} need attention
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
              // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
              <button
                key={key}
                onClick={() => setActiveWindow(key)}
                aria-pressed={activeWindow === key}
                className={`px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
                  activeWindow === key
                    ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                    : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
                }`}
              >
                {label}
              </button>
            ))}
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
          ) : !hasActivity || !digest ? (
            <div className="flex items-center gap-2 text-xs text-neutral-600 py-1">
              <CheckCircle className="w-3.5 h-3.5" />
              All clear — nothing to catch up on
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {(digest.narrative_summary || digest.trend) && (
                <div className="flex items-start gap-2 mb-3">
                  {digest.narrative_summary && (
                    <p className="text-sm text-neutral-300 leading-relaxed flex-1">
                      {activeWindow === 'last_visit' && digest.window_hours && (
                        <span className="text-neutral-400">Since you last checked ({digest.window_hours}h ago): </span>
                      )}
                      {digest.narrative_summary}
                    </p>
                  )}
                  {digest.trend && (
                    <span
                      className={`shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full border ${
                        digest.trend === 'improving'
                          ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20'
                          : digest.trend === 'declining'
                            ? 'bg-red-500/15 text-red-400 border-red-500/20'
                            : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30'
                      }`}
                      aria-label={`Trend: ${digest.trend === 'improving' ? 'Improving' : digest.trend === 'declining' ? 'Declining' : 'Stable'}`}
                    >
                      {digest.trend === 'improving' ? 'Improving' : digest.trend === 'declining' ? 'Declining' : 'Stable'}
                    </span>
                  )}
                </div>
              )}
              {digest.latest_cycle && (
                <div className="flex flex-col gap-1 p-2 rounded border border-neutral-800 bg-neutral-900/40">
                  <div className="flex items-center gap-2 text-[11px]">
                    <span className="font-semibold text-neutral-400">Latest strategic cycle</span>
                    {digest.latest_cycle.outcome && (
                      <span className="px-1.5 py-0.5 rounded border text-[10px] bg-neutral-800/60 text-neutral-300 border-neutral-700">
                        {digest.latest_cycle.outcome}
                      </span>
                    )}
                    {digest.latest_cycle.completed_at && (
                      <span className="text-[10px] text-neutral-600 font-mono">{timeAgo(digest.latest_cycle.completed_at)}</span>
                    )}
                    <span className="text-[10px] text-neutral-500">
                      +{digest.latest_cycle.directions_created ?? 0} / −{digest.latest_cycle.directions_retired ?? 0} directions
                    </span>
                    {digest.latest_cycle.vision_updated && (
                      <span className="text-[10px] text-cyan-400">vision updated</span>
                    )}
                  </div>
                  {digest.latest_cycle.summary_text && (
                    <div className="text-[11px] text-neutral-400 leading-relaxed">
                      {cycleSummaryExpanded || digest.latest_cycle.summary_text.length <= 200
                        ? digest.latest_cycle.summary_text
                        : digest.latest_cycle.summary_text.slice(0, 200) + '…'}
                      {digest.latest_cycle.summary_text.length > 200 && (
                        <button
                          type="button"
                          onClick={() => setCycleSummaryExpanded(v => !v)}
                          className="ml-1 text-[10px] text-cyan-500 hover:text-cyan-400"
                        >
                          {cycleSummaryExpanded ? 'less' : 'more'}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
              {/* Pipeline regressions — compact inline variant */}
              {selectedProjectId && (
                <ErrorBoundary level="widget">
                  <PipelineRegressionsPanel projectId={selectedProjectId} />
                </ErrorBoundary>
              )}
              {/* Stats row */}
              <div className="flex flex-wrap gap-x-3 gap-y-2 text-[11px] text-neutral-400">
                {digest.success_rate != null && (
                  <span
                    className={`flex items-center gap-1 ${digest.success_rate >= 0.8 ? 'text-emerald-400' : digest.success_rate >= 0.5 ? 'text-amber-400' : 'text-red-400'}`}
                    aria-label={`Success rate: ${Math.round(digest.success_rate * 100)}% (${digest.success_rate >= 0.8 ? 'healthy' : digest.success_rate >= 0.5 ? 'warning' : 'critical'})`}
                  >
                    {Math.round(digest.success_rate * 100)}% success
                    {digest.success_rate_delta != null && (
                      <span className={digest.success_rate_delta > 0 ? 'text-emerald-400' : digest.success_rate_delta < 0 ? 'text-red-400' : 'text-neutral-500'}>
                        {digest.success_rate_delta > 0 ? '+' : ''}{Math.round(digest.success_rate_delta * 100)}%
                      </span>
                    )}
                    {digest.trend_history && digest.trend_history.length >= 3 && (
                      <div className="flex items-end gap-px h-3 ml-1">
                        {digest.trend_history.map((p, i) => {
                          const rate = p.success_rate ?? 0;
                          const color = rate >= 0.8 ? 'bg-emerald-400' : rate >= 0.5 ? 'bg-amber-400' : 'bg-red-400';
                          const dateLabel = (() => {
                            const d = parseTimestamp(p.period_end);
                            return isNaN(d.getTime()) ? p.period_end : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
                          })();
                          const ratePart = p.success_rate == null ? 'no data' : `${Math.round(p.success_rate * 100)}%`;
                          const titleParts = [`Success: ${ratePart}`];
                          if (p.first_attempt_success_rate != null) {
                            titleParts.push(`1st attempt: ${Math.round(p.first_attempt_success_rate * 100)}%`);
                          }
                          if (p.period_cost_usd != null) {
                            titleParts.push(`Cost: $${p.period_cost_usd.toFixed(2)}`);
                          }
                          const title = `${dateLabel}: ${titleParts.join(' | ')} (${p.completed} done, ${p.failed} failed)`;
                          return <div key={i} title={title} className={`w-1 rounded-sm ${p.success_rate == null ? 'bg-neutral-700' : color}`} style={{height: `${Math.max(rate * 100, 8)}%`}} />;
                        })}
                      </div>
                    )}
                  </span>
                )}
                {digest.first_attempt_success_rate && digest.first_attempt_success_rate.sample_size >= 5 && (() => {
                  const fas = digest.first_attempt_success_rate;
                  if (!fas) return null;
                  const tone = (r: number | null) => r == null ? 'text-neutral-500' : r >= 0.8 ? 'text-emerald-400' : r >= 0.5 ? 'text-amber-400' : 'text-red-400';
                  const compareTone = (w: number | null, wo: number | null) =>
                    w == null || wo == null ? 'text-neutral-500' : w > wo ? 'text-emerald-400' : w < wo ? 'text-red-400' : 'text-amber-400';
                  const fmt = (r: number | null) => r == null ? '—' : `${Math.round(r * 100)}%`;
                  return (
                    <>
                      {fas.overall != null && (
                        <span
                          className={`flex items-center gap-1 ${tone(fas.overall)}`}
                          title={`First-attempt success rate (n=${fas.sample_size})${fas.trend ? ` — trend: ${fas.trend}` : ''}`}
                        >
                          {Math.round(fas.overall * 100)}% 1st-attempt
                          {fas.trend === 'improving' && <span className="text-emerald-400">↑</span>}
                          {fas.trend === 'declining' && <span className="text-red-400">↓</span>}
                          {fas.trend === 'stable' && <span className="text-neutral-500">→</span>}
                        </span>
                      )}
                      {(fas.with_playbook != null || fas.without_playbook != null) && (
                        <span
                          className={compareTone(fas.with_playbook, fas.without_playbook)}
                          title="First-attempt success with vs without playbook"
                        >
                          Playbook: {fmt(fas.with_playbook)} vs {fmt(fas.without_playbook)} without
                        </span>
                      )}
                      {(fas.with_arch_context != null || fas.without_arch_context != null) && (
                        <span
                          className={compareTone(fas.with_arch_context, fas.without_arch_context)}
                          title="First-attempt success with vs without arch context"
                        >
                          Arch context: {fmt(fas.with_arch_context)} vs {fmt(fas.without_arch_context)} without
                        </span>
                      )}
                      {fas.by_scope && Object.keys(fas.by_scope).length > 0 && (() => {
                        const order = ['small', 'medium', 'large'];
                        const byScope = fas.by_scope;
                        if (!byScope) return null;
                        const entries = Object.entries(byScope)
                          .filter(([, v]) => typeof v === 'number')
                          .sort(([a], [b]) => {
                            const ai = order.indexOf(a); const bi = order.indexOf(b);
                            return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
                          });
                        if (entries.length === 0) return null;
                        return (
                          <span
                            className="flex items-center gap-1 text-neutral-400"
                            title="First-attempt success rate by task scope"
                          >
                            <span>Scope:</span>
                            {entries.map(([scope, rate], i) => (
                              <span key={scope} className={tone(rate)}>
                                {scope}: {Math.round(rate * 100)}%{i < entries.length - 1 ? ' |' : ''}
                              </span>
                            ))}
                          </span>
                        );
                      })()}
                      {fas.first_attempt_by_review_category && Object.keys(fas.first_attempt_by_review_category).length > 0 && (() => {
                        const fbrc = fas.first_attempt_by_review_category;
                        if (!fbrc) return null;
                        const entries = Object.entries(fbrc)
                          .filter(([, v]) => typeof v === 'number' && v > 0)
                          .sort((a, b) => b[1] - a[1])
                          .slice(0, 3);
                        if (entries.length === 0) return null;
                        return (
                          <span
                            className="flex items-center gap-1 text-neutral-400"
                            title="Top review categories responsible for first-attempt rejections"
                          >
                            <span>Rejections:</span>
                            {entries.map(([cat, frac]) => (
                              <span
                                key={cat}
                                className="px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-300"
                              >
                                {cat} {Math.round(frac * 100)}%
                              </span>
                            ))}
                          </span>
                        );
                      })()}
                    </>
                  );
                })()}
                {digest.pipeline_acceptance_rate != null && (() => {
                  const par = digest.pipeline_acceptance_rate;
                  if (par == null) return null;
                  const pct = Math.round(par * 100);
                  const tone = pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
                  return (
                    <span className={tone} title="PRs merged / PRs decided (merged + rejected)">
                      {pct}% accepted
                    </span>
                  );
                })()}
                {digest.pipeline_conversion_rate != null && (() => {
                  const pcr = digest.pipeline_conversion_rate;
                  if (pcr == null) return null;
                  const pct = Math.round(pcr * 100);
                  const tone = pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
                  return (
                    <span className={tone} title="PRs merged / tasks created">
                      {pct}% conv
                    </span>
                  );
                })()}
                {digest.overall_product_ratio != null && (() => {
                  const opr = digest.overall_product_ratio;
                  if (opr == null) return null;
                  const pct = Math.round(opr * 100);
                  const tone = opr >= 0.6 ? 'text-emerald-400' : opr >= 0.4 ? 'text-amber-400' : 'text-slate-400';
                  return (
                    <span className={tone} title="Fraction of tasks targeting product features vs structural work">
                      {pct}% product focus
                    </span>
                  );
                })()}
                {digest.overall_type_distribution && Object.keys(digest.overall_type_distribution).length > 0 && (
                  <span className="flex items-center gap-1" title="Task type distribution across all directions">
                    {Object.entries(digest.overall_type_distribution)
                      .sort((a, b) => b[1] - a[1])
                      .map(([type, count]) => {
                        const style = TASK_TYPE_STYLES[type];
                        return (
                          <span
                            key={type}
                            className={`${style?.bg ?? 'bg-neutral-500/10 border-neutral-500/20'} ${style?.text ?? 'text-neutral-400'} border px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider text-[10px]`}
                          >
                            {style?.label ?? type} {count}
                          </span>
                        );
                      })}
                  </span>
                )}
                {digest.pending_approval_count > 0 && (
                  <span className="flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3 text-amber-400" />
                    {digest.pending_approval_count} pending approval
                  </span>
                )}
                {digest.completed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <CheckCircle className="w-3 h-3 text-emerald-500" />
                    {digest.completed_count} completed
                    {digest.completed_delta != null && digest.completed_delta !== 0 && (
                      <span
                        className={digest.completed_delta > 0 ? 'text-emerald-400' : 'text-red-400'}
                        title={digest.prev_completed_count != null ? `was ${digest.prev_completed_count} last period` : undefined}
                      >
                        {digest.completed_delta > 0 ? '↑' : '↓'}
                      </span>
                    )}
                  </span>
                )}
                {digest.failed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <XCircle className="w-3 h-3 text-red-500" />
                    {digest.failed_count} failed
                    {digest.failed_delta != null && digest.failed_delta !== 0 && (
                      <span
                        className={digest.failed_delta > 0 ? 'text-red-400' : 'text-emerald-400'}
                        title={digest.prev_failed_count != null ? `was ${digest.prev_failed_count} last period` : undefined}
                      >
                        {digest.failed_delta > 0 ? '↑' : '↓'}
                      </span>
                    )}
                    {digest.total_failed_cost_usd != null && digest.total_failed_cost_usd > 0 && (
                      <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono">
                        ·<DollarSign className="w-2.5 h-2.5" />{digest.total_failed_cost_usd.toFixed(2)} wasted
                      </span>
                    )}
                  </span>
                )}
                {digest.total_estimated_cost_usd != null && digest.total_estimated_cost_usd > 0 && (
                  <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono" title="Total estimated spend">
                    <DollarSign className="w-2.5 h-2.5" />{digest.total_estimated_cost_usd.toFixed(2)} total
                    {(() => {
                      if (costs.length < 2) return null;
                      const curr = costs[costs.length - 1];
                      const prev = costs[costs.length - 2];
                      const delta = curr - prev;
                      if (Math.abs(delta) < 0.01) return null;
                      const up = delta > 0;
                      return (
                        <span
                          className={up ? "text-amber-600" : "text-emerald-600"}
                          title={`Period cost change: $${prev.toFixed(2)} → $${curr.toFixed(2)}`}
                        >
                          {up ? "↑" : "↓"}${Math.abs(delta).toFixed(2)}
                        </span>
                      );
                    })()}
                  </span>
                )}
                {(digest.spot_cost_usd != null || digest.on_demand_cost_usd != null) && (
                  (digest.spot_cost_usd ?? 0) + (digest.on_demand_cost_usd ?? 0) > 0
                ) && (
                  <span
                    className="inline-flex items-center gap-0.5 text-neutral-500 font-mono"
                    title="Spot vs on-demand compute cost split"
                  >
                    Spot: ${(digest.spot_cost_usd ?? 0).toFixed(2)} / On-demand: ${(digest.on_demand_cost_usd ?? 0).toFixed(2)}
                  </span>
                )}
                {(() => {
                  if (!digest.direction_progress || digest.direction_progress.length === 0) return null;
                  const order = ['small', 'medium', 'large'];
                  const entries = order.filter(s => directionCostTotals[s] != null);
                  if (entries.length === 0) return null;
                  const title = `Cost by scope: ${entries.map(s => `${s} $${directionCostTotals[s].toFixed(2)}`).join(' · ')}`;
                  return (
                    <span className="inline-flex items-center gap-1 font-mono text-neutral-500" title={title}>
                      {entries.map(s => (
                        <span key={s} className="inline-flex items-center gap-0.5 rounded bg-neutral-100 px-1 py-0.5 text-[10px]">
                          {s} ${directionCostTotals[s].toFixed(2)}
                        </span>
                      ))}
                    </span>
                  );
                })()}
                {digest.provisioning_overhead_pct != null && digest.provisioning_overhead_pct > 0 && (() => {
                  const pop = digest.provisioning_overhead_pct;
                  if (pop == null) return null;
                  const pctDisplay = pop * 100;
                  const tone = pctDisplay < 10
                    ? 'text-emerald-500'
                    : pctDisplay <= 25
                      ? 'text-amber-500'
                      : 'text-red-500';
                  const overheadUsd = digest.provisioning_overhead_usd ?? 0;
                  return (
                    <span
                      className={`inline-flex items-center gap-0.5 font-mono ${tone}`}
                      title={`Provisioning overhead: $${overheadUsd.toFixed(2)} (${pctDisplay.toFixed(1)}%)`}
                    >
                      Provisioning overhead: {pctDisplay.toFixed(1)}%
                    </span>
                  );
                })()}
                {digest.daily_cost_usd != null && digest.daily_cost_usd > 0 && (() => {
                  const spent = digest.daily_cost_usd;
                  if (spent == null) return null;
                  const ceiling = digest.daily_cost_ceiling_usd ?? 0;
                  const ratio = ceiling > 0 ? spent / ceiling : 0;
                  const tone = ceiling <= 0
                    ? 'text-neutral-500'
                    : ratio >= 0.9
                      ? 'text-red-500'
                      : ratio >= 0.7
                        ? 'text-amber-500'
                        : 'text-emerald-500';
                  const title = ceiling > 0
                    ? `Today: $${spent.toFixed(2)} of $${ceiling.toFixed(2)} daily ceiling (${Math.round(ratio * 100)}%)`
                    : `Today: $${spent.toFixed(2)} (no daily ceiling set)`;
                  return (
                    <span className={`inline-flex items-center gap-0.5 font-mono ${tone}`} title={title}>
                      <DollarSign className="w-2.5 h-2.5" />{spent.toFixed(2)} today
                      {ceiling > 0 && <span className="text-neutral-500">/{ceiling.toFixed(0)}</span>}
                    </span>
                  );
                })()}
                {digest.research_cycles > 0 && (
                  <span>{digest.research_cycles} research cycle{digest.research_cycles !== 1 ? 's' : ''}</span>
                )}
                {digest.prs_opened > 0 && (
                  <span>{digest.prs_opened} PR{digest.prs_opened !== 1 ? 's' : ''} opened</span>
                )}
                {digest.running_count > 0 && (
                  <span className="flex items-center gap-1">
                    <Loader2 className="w-3 h-3 text-neutral-500 animate-spin" />
                    {digest.running_count} running
                  </span>
                )}
                {digest.queued_count > 0 && (
                  <span>{digest.queued_count} queued</span>
                )}
              </div>

              {/* Pipeline filter funnel */}
              {digest.pipeline_filter_funnel && (digest.pipeline_filter_funnel.findings_produced ?? 0) > 0 && (() => {
                const ff = digest.pipeline_filter_funnel;
                if (!ff) return null;
                const produced = ff.findings_produced ?? 0;
                const created = ff.tasks_created ?? 0;
                const pct = produced > 0 ? Math.round((created / produced) * 100) : 0;
                return (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-400">
                    <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Research yield</span>
                    <span>
                      <span className="font-semibold text-neutral-200">{created}</span>
                      <span className="text-neutral-500"> / {produced} findings &rarr; tasks </span>
                      <span className="font-semibold text-neutral-200">({pct}%)</span>
                    </span>
                    {drops.slice(0, 3).map(d => (
                      <span key={d.key} className="text-neutral-500" title={d.label}>
                        {d.label} <span className="font-mono text-neutral-400">-{d.v}</span>
                      </span>
                    ))}
                  </div>
                );
              })()}

              {/* Direction progress */}
              {digest.direction_progress && digest.direction_progress.length > 0 && (
                <ErrorBoundary level="widget">
                  <DirectionProgressSection directions={digest.direction_progress} medianCostPerMergedPr={digest.median_cost_per_merged_pr} />
                </ErrorBoundary>
              )}

              {/* Needs attention */}
              {(digest.needs_attention ?? []).length > 0 && (() => {
                const hiddenItems = digest.needs_attention.filter(item => isHidden(item.task_id));
                const countLabel = hiddenItems.length > 0
                  ? `${visibleItems.length} visible / ${digest.needs_attention.length} total`
                  : String(digest.needs_attention.length);
                return (
                  <div className="border border-amber-500/30 rounded-lg p-3 flex flex-col gap-2">
                    <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-400 uppercase tracking-widest">
                      <AlertTriangle className="w-3 h-3" />
                      <span className="flex-1">Needs attention ({countLabel})</span>
                      {onBulkApprove && approvalItems.length >= 2 && (
                        <button
                          className="text-[10px] font-semibold px-2 py-4 rounded border flex items-center gap-1 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20 normal-case tracking-normal"
                          onClick={() => handleBulkApprove(approvalItems.map(i => i.task_id))}
                        >
                          <Check className="w-3 h-3" />
                          Approve all ({approvalItems.length})
                        </button>
                      )}
                      {onBulkRetry && failedItems.length >= 2 && (
                        <button
                          className="text-[10px] font-semibold px-2 py-4 rounded border flex items-center gap-1 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20 normal-case tracking-normal"
                          onClick={() => handleBulkRetry(failedItems.map(i => i.task_id))}
                        >
                          <RotateCcw className="w-3 h-3" />
                          Retry all ({failedItems.length})
                        </button>
                      )}
                    </div>
                    <div className="flex flex-col gap-1.5">
                      {visibleItems.map(item => (
                        <div key={item.task_id} className="flex flex-col gap-1">
                          {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                          <div
                            role="button"
                            tabIndex={0}
                            className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                            onClick={() => onSelectTask({ id: item.task_id })}
                            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: item.task_id }); } }}
                          >
                            <span className="min-w-0 flex-1 truncate text-neutral-300">{item.title}</span>
                            {item.direction_text && (
                              <span
                                className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[12rem]"
                                title={item.direction_text}
                              >
                                {item.direction_text.length > 40 ? item.direction_text.slice(0, 40) + '…' : item.direction_text}
                              </span>
                            )}
                            {item.scope && (
                              <span
                                className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                                title={`scope: ${item.scope}`}
                              >
                                {item.scope}
                              </span>
                            )}
                            <span className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${
                              item.reason === 'failed'
                                ? 'bg-red-500/10 text-red-400'
                                : 'bg-amber-500/10 text-amber-400'
                            }`}>
                              {item.reason === 'failed' ? 'Failed' : 'Approval required'}
                            </span>
                            {item.reason === 'failed' && item.failure_class && FAILURE_CLASS_CONFIG[item.failure_class] && (
                              <span
                                className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${FAILURE_CLASS_CONFIG[item.failure_class].color}`}
                                title={FAILURE_CLASS_CONFIG[item.failure_class].hint}
                              >
                                {FAILURE_CLASS_CONFIG[item.failure_class].label}
                              </span>
                            )}
                            {item.reason === 'failed' && typeof item.merge_retries_saved === 'number' && item.merge_retries_saved > 0 && (
                              <span
                                className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                                title="Merge failed with identical error repeatedly — early exit saved these retry attempts"
                              >
                                saved {item.merge_retries_saved} {item.merge_retries_saved === 1 ? 'retry' : 'retries'}
                              </span>
                            )}
                            {item.reason === 'failed' && item.pr_url && isSafeUrl(item.pr_url) && (
                              <a
                                href={item.pr_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={e => e.stopPropagation()}
                                className="shrink-0 text-[9px] text-emerald-400 hover:text-emerald-300 font-medium"
                              >
                                {item.pr_url.match(/\/(\d+)\/?$/)?.[1] ? `PR #${item.pr_url.match(/\/(\d+)\/?$/)?.[1]}` : 'PR'}
                              </a>
                            )}
                            <WaitingBadge hours={item.waiting_hours} />
                            {item.reason === 'approval_required' && onApprove && (
                              <button
                                className="text-[9px] font-semibold px-2 py-4 rounded border flex items-center gap-0.5 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20"
                                onClick={e => { e.stopPropagation(); handleApprove(item.task_id); }}
                              >
                                <Check className="w-2.5 h-2.5" />
                                Approve
                              </button>
                            )}
                            {item.reason === 'failed' && onRetry && (
                              <button
                                className="text-[9px] font-semibold px-2 py-4 rounded border flex items-center gap-0.5 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20"
                                onClick={e => { e.stopPropagation(); handleRetry(item.task_id); }}
                              >
                                <RotateCcw className="w-2.5 h-2.5" />
                                Retry
                              </button>
                            )}
                            {/* Dismiss/snooze button */}
                            <div className="relative shrink-0" onClick={e => e.stopPropagation()}>
                              {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                              <button
                                className="text-neutral-600 hover:text-neutral-400 transition-colors px-1 leading-none text-sm"
                                title="Dismiss or snooze"
                                aria-label="Dismiss or snooze this item"
                                onClick={() => setOpenDropdown(openDropdown === item.task_id ? null : item.task_id)}
                                onKeyDown={e => { if (e.key === 'Escape') setOpenDropdown(null); }}
                              >
                                ×
                              </button>
                              {openDropdown === item.task_id && (
                                <div
                                  ref={dropdownRef}
                                  role="menu"
                                  className="absolute right-0 top-full mt-0.5 z-10 bg-neutral-900 border border-neutral-700 rounded shadow-lg py-0.5 min-w-[110px]"
                                  onKeyDown={e => { if (e.key === 'Escape') setOpenDropdown(null); }}
                                >
                                  {(['Dismiss', 'Snooze 4h', 'Snooze 12h', 'Snooze 24h'] as const).map(opt => (
                                    <button
                                      key={opt}
                                      role="menuitem"
                                      className="block w-full text-left text-[11px] px-3 py-1 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-100 transition-colors"
                                      onClick={() => {
                                        if (opt === 'Dismiss') dismissItem(item.task_id);
                                        else if (opt === 'Snooze 4h') snoozeItem(item.task_id, 4);
                                        else if (opt === 'Snooze 12h') snoozeItem(item.task_id, 12);
                                        else snoozeItem(item.task_id, 24);
                                      }}
                                    >
                                      {opt}
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>
                          </div>
                          {item.reason === 'approval_required' && item.approval_reason && (
                            <div className="flex items-center gap-1.5 px-2">
                              <span className="text-[10px] text-neutral-500 italic truncate">
                                {item.approval_reason.length > 100 ? item.approval_reason.slice(0, 100) + '…' : item.approval_reason}
                              </span>
                              {item.touched_critical && item.touched_critical.length > 0 && (
                                <span className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                                  critical files
                                </span>
                              )}
                            </div>
                          )}
                          {item.error_snippet && (
                            <div className="px-2">
                              <button
                                onClick={() => toggleSnippet(item.task_id)}
                                aria-expanded={expandedSnippets.has(item.task_id)}
                                aria-label={expandedSnippets.has(item.task_id) ? 'Hide error details' : 'Show error details'}
                                className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
                              >
                                {expandedSnippets.has(item.task_id) ? 'hide error ▲' : 'show error ▼'}
                              </button>
                              {expandedSnippets.has(item.task_id) && (
                                <pre className="mt-1 text-[10px] text-red-400/80 bg-red-950/20 border border-red-900/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
                                  {item.error_snippet}
                                </pre>
                              )}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                    {hiddenItems.length > 0 && (
                      <div className="mt-1">
                        <button
                          className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
                          onClick={() => setShowDismissed(v => !v)}
                        >
                          {showDismissed ? 'Hide dismissed' : `Show ${hiddenItems.length} dismissed`}
                        </button>
                        {showDismissed && (
                          <div className="flex flex-col gap-1.5 mt-1.5">
                            {hiddenItems.map(item => {
                              const entry = dismissed[item.task_id];
                              return (
                                <div key={item.task_id} className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/50 px-2 py-1.5 rounded opacity-50">
                                  <span className="min-w-0 flex-1 truncate text-neutral-400">{item.title}</span>
                                  {entry?.type === 'snooze' && (
                                    <span className="shrink-0 text-[9px] font-mono text-neutral-500">
                                      {formatSnoozeRemaining((entry as { type: 'snooze'; until: number }).until)}
                                    </span>
                                  )}
                                  {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                                  <button
                                    className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border bg-neutral-700/30 text-neutral-400 hover:bg-neutral-700/60 border-neutral-700/40 transition-colors"
                                    aria-label="Restore this item"
                                    onClick={() => restoreItem(item.task_id)}
                                  >
                                    Restore
                                  </button>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* In progress */}
              {digest.active_count > 0 && (
                <div>
                  <button
                    onClick={() => setActiveExpanded(v => !v)}
                    aria-expanded={activeExpanded}
                    className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                  >
                    {activeExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    <Loader2 className="w-3 h-3 text-neutral-500" />
                    In progress ({digest.active_count})
                  </button>
                  {activeExpanded && (
                    <div className="flex flex-col gap-1.5">
                      {(digest.active_tasks ?? []).map(t => (
                        // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                        <div
                          key={t.task_id}
                          role="button"
                          tabIndex={0}
                          className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                          onClick={() => onSelectTask({ id: t.task_id })}
                          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: t.task_id }); } }}
                        >
                          <span className="min-w-0 flex-1 truncate text-neutral-400">{t.title}</span>
                          {t.direction_text && (
                            <span
                              className="shrink-0 inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 max-w-[180px] truncate"
                              title={t.direction_text}
                            >
                              <Compass className="w-2.5 h-2.5 shrink-0" />
                              <span className="truncate">{t.direction_text.slice(0, 30)}{t.direction_text.length > 30 ? '…' : ''}</span>
                            </span>
                          )}
                          {t.task_type && (
                            <TaskTypeBadge taskType={t.task_type as TaskType} />
                          )}
                          {t.scope && (
                            <span
                              className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                              title={`scope: ${t.scope}`}
                            >
                              {t.scope}
                            </span>
                          )}
                          {t.review_iteration != null && t.review_iteration > 0 && t.review_cap != null && t.review_cap > 0 && (() => {
                            const reviewIter = t.review_iteration;
                            const reviewCap = t.review_cap;
                            if (reviewIter == null || reviewCap == null) return null;
                            const ratio = reviewIter / reviewCap;
                            const colorCls = ratio > 0.9
                              ? "bg-red-500/10 text-red-400 border-red-500/20"
                              : ratio > 0.7
                                ? "bg-amber-500/10 text-amber-400 border-amber-500/20"
                                : "bg-neutral-700/20 text-neutral-400 border-neutral-700/30";
                            return (
                              <span
                                className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${colorCls}`}
                                title={`review iterations: ${t.review_iteration}/${t.review_cap}`}
                              >
                                {t.review_iteration}/{t.review_cap} iter
                              </span>
                            );
                          })()}
                          {t.running_cost_usd != null && t.running_cost_usd > 0 && (
                            <span className="shrink-0 inline-flex items-center gap-0.5 text-[9px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                              <DollarSign className="w-2.5 h-2.5" />
                              {t.running_cost_usd.toFixed(2)}
                            </span>
                          )}
                          <span className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-500">
                            {t.status}
                          </span>
                          {t.started_at && (
                            <span className="shrink-0 text-[10px] text-neutral-600 font-mono">{timeAgo(t.started_at)}</span>
                          )}
                        </div>
                      ))}
                      {digest.active_count > (digest.active_tasks ?? []).length && (
                        <div className="text-[10px] text-neutral-600 px-2">
                          +{digest.active_count - (digest.active_tasks ?? []).length} more
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Failure taxonomy root-cause pills */}
              {digest.failure_taxonomy_distribution && digest.failure_taxonomy_distribution.total_failures > 0 && (() => {
                const ftd = digest.failure_taxonomy_distribution;
                if (!ftd) return null;
                const costByTaxonomy: Record<string, number> = {};
                const costByClass = digest.cost_by_failure_class ?? {};
                for (const g of digest.failure_groups ?? []) {
                  const groupCost = costByClass[g.failure_class] ?? 0;
                  if (!groupCost || g.tasks.length === 0) continue;
                  const perTask = groupCost / g.tasks.length;
                  for (const t of g.tasks) {
                    if (!t.taxonomy) continue;
                    costByTaxonomy[t.taxonomy] = (costByTaxonomy[t.taxonomy] ?? 0) + perTask;
                  }
                }
                return (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Root cause</span>
                    {TAXONOMY_ORDER.map(cat => {
                      const entry = ftd.categories[cat];
                      if (!entry || entry.count === 0) return null;
                      const cost = costByTaxonomy[cat];
                      const trend = trendFor(cat);
                      const isActive = activeTaxonomyFilter === cat;
                      const isDominant = ftd.dominant_category === cat;
                      return (
                        // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                        <button
                          key={cat}
                          type="button"
                          onClick={() => {
                            setActiveTaxonomyFilter(isActive ? null : cat);
                            setActiveStageFilter(null);
                            setFailuresExpanded(true);
                          }}
                          aria-pressed={isActive}
                          className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded border transition-all ${TAXONOMY_COLORS[cat]} ${isActive ? 'ring-2 ring-white/60' : isDominant ? 'ring-1 ring-amber-300/70 shadow-[0_0_0_1px_rgba(252,211,77,0.35)]' : 'hover:opacity-80'}`}
                          title={`${TAXONOMY_LABELS[cat]}: ${entry.count} (${Math.round(entry.percentage)}%)${cost ? ` · $${cost.toFixed(2)}` : ''}${isDominant ? ' — dominant category' : ''} — click to ${isActive ? 'clear' : 'filter'}`}
                        >
                          {isDominant && <span aria-label="dominant" className="text-amber-300">★</span>}
                          {TAXONOMY_LABELS[cat]}
                          <span className="font-bold">{entry.count}</span>
                          <span className={`font-bold ${trend.color}`}>{trend.arrow}</span>
                          {cost != null && cost > 0 && (
                            <span className="inline-flex items-center font-mono font-normal opacity-80">
                              ·<DollarSign className="w-2.5 h-2.5" />{cost.toFixed(2)}
                            </span>
                          )}
                        </button>
                      );
                    })}
                    {activeTaxonomyFilter && (
                      <button
                        type="button"
                        onClick={() => setActiveTaxonomyFilter(null)}
                        className="text-[10px] text-neutral-400 hover:text-neutral-200 underline underline-offset-2"
                      >
                        clear
                      </button>
                    )}
                  </div>
                );
              })()}

              {/* Failure stage distribution pills */}
              {digest.failure_stage_distribution && digest.failure_stage_distribution.total_classified > 0 && (() => {
                const fsd = digest.failure_stage_distribution;
                if (!fsd) return null;
                return (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Pipeline stage</span>
                  {STAGE_ORDER.map(stage => {
                    const count = fsd.stages[stage];
                    if (!count) return null;
                    const isActive = activeStageFilter === stage;
                    const isDominant = fsd.dominant_stage === stage;
                    return (
                      // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                      <button
                        key={stage}
                        type="button"
                        onClick={() => {
                          setActiveStageFilter(isActive ? null : stage);
                          setActiveTaxonomyFilter(null);
                          setFailuresExpanded(true);
                        }}
                        aria-pressed={isActive}
                        className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded border transition-all ${STAGE_COLORS[stage] ?? 'bg-neutral-700/20 text-neutral-400 border-neutral-700/30'} ${isActive ? 'ring-2 ring-white/60' : isDominant ? 'ring-1 ring-amber-300/70 shadow-[0_0_0_1px_rgba(252,211,77,0.35)]' : 'hover:opacity-80'}`}
                        title={`${stage}: ${count}${isDominant ? ' — dominant stage' : ''} — click to ${isActive ? 'clear' : 'filter'}`}
                      >
                        {isDominant && <span aria-label="dominant" className="text-amber-300">★</span>}
                        {stage}
                        <span className="font-bold">{count}</span>
                      </button>
                    );
                  })}
                  {activeStageFilter && (
                    <button
                      type="button"
                      onClick={() => setActiveStageFilter(null)}
                      className="text-[10px] text-neutral-400 hover:text-neutral-200 underline underline-offset-2"
                    >
                      clear
                    </button>
                  )}
                </div>
                );
              })()}

              {/* Scope conversion rate pills */}
              {digest.scope_conversion_rates && Object.keys(digest.scope_conversion_rates).length > 0 && (() => {
                const scr = digest.scope_conversion_rates;
                if (!scr) return null;
                return (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope conversion</span>
                  {['small', 'medium', 'large']
                    .filter(s => scr[s] != null)
                    .map(scope => {
                      const { created, merged, calibrated_in = 0, calibrated_out = 0 } = scr[scope];
                      const recalCount = (calibrated_in || 0) + (calibrated_out || 0);
                      const rate = created > 0 ? merged / created : null;
                      const prev = digest.prev_scope_conversion_rates?.[scope];
                      const prevRate = prev && prev.created > 0 ? prev.merged / prev.created : null;
                      let trend: 'improving' | 'declining' | 'stable' | null = null;
                      if (rate != null && prevRate != null) {
                        const delta = rate - prevRate;
                        trend = delta > 0.05 ? 'improving' : delta < -0.05 ? 'declining' : 'stable';
                      }
                      const arrow = trend === 'improving' ? '↑' : trend === 'declining' ? '↓' : trend === 'stable' ? '→' : '';
                      const arrowColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
                      const color = rate == null
                        ? 'bg-neutral-500/10 text-neutral-400 border-neutral-500/20'
                        : rate >= 0.6 ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                        : rate >= 0.3 ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                        : 'bg-red-500/10 text-red-400 border-red-500/20';
                      return (
                        <span
                          key={scope}
                          className={`text-[10px] border px-1.5 py-0.5 rounded inline-flex items-center gap-1 ${color}`}
                          title={recalCount > 0 ? `Recalibrations: ${calibrated_in} in, ${calibrated_out} out` : undefined}
                        >
                          {scope}: {rate != null ? `${Math.round(rate * 100)}%` : '--'} ({merged}/{created})
                          {arrow && <span className={arrowColor}>{arrow}</span>}
                          {recalCount > 0 && <span className="text-neutral-400">· {recalCount} recal</span>}
                        </span>
                      );
                    })}
                </div>
                );
              })()}

              {/* Scope mismatch diagnostics */}
              {digest.scope_mismatch_summary && digest.scope_mismatch_summary.total_calibrated > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope mismatch</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest.scope_mismatch_summary.total_calibrated} calibrated
                    </span>
                    {digest.scope_mismatch_summary.calibrated_success_rate != null && (
                      <span className="text-[10px] text-neutral-500">
                        · {Math.round(digest.scope_mismatch_summary.calibrated_success_rate * 100)}% success
                      </span>
                    )}
                    {Object.entries(digest.scope_mismatch_summary.by_original_scope).map(([orig, b]) => (
                      <span key={orig} className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 px-1.5 py-0.5 rounded">
                        {orig}: {b.total}
                      </span>
                    ))}
                  </div>
                  {digest.scope_mismatch_summary.top_directions.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1 max-w-full">
                      {digest.scope_mismatch_summary.top_directions.map(d => (
                        <span key={d.direction_id} className="text-[10px] text-neutral-500 truncate">
                          · {(d.direction_text || d.direction_id).slice(0, 60)}{' '}
                          <span className="text-cyan-400">{d.calibrated_count}/{d.total_tasks} ({Math.round(d.calibration_rate * 100)}%)</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Rejection pattern summary */}
              {digest.rejection_pattern_summary && digest.rejection_pattern_summary.total_rejected > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Rejection patterns</span>
                    <button
                      type="button"
                      onClick={() => setFailuresExpanded(true)}
                      className="text-[10px] text-neutral-400 hover:text-purple-300 underline decoration-dotted underline-offset-2"
                      title="Expand recent failures to find rejected tasks"
                    >
                      {digest.rejection_pattern_summary.total_rejected} rejected
                    </button>
                  </div>
                  {digest.rejection_pattern_summary.top_notes.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1 max-w-full">
                      {digest.rejection_pattern_summary.top_notes.map((n) => (
                        <span key={n.snippet} className="text-[10px] text-neutral-500 truncate">
                          · {n.snippet.slice(0, 100)}{n.snippet.length > 100 ? '…' : ''}{' '}
                          <span className="text-purple-400">×{n.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Systemic errors — clusters of failures with matching error fingerprints */}
              {(digest.recurring_error_clusters ?? []).length > 0 && (() => {
                const recurringClusters = digest.recurring_error_clusters ?? [];
                return (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Systemic errors</span>
                    <span className="text-[10px] text-neutral-400">
                      {recurringClusters.length} cluster{recurringClusters.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1 ml-1 max-w-full">
                    {(() => {
                      const dirTextById = new Map<string, string>(
                        (digest.direction_progress ?? []).map(dp => [dp.direction_id, dp.direction_text || dp.direction_id]),
                      );
                      return recurringClusters.map(cluster => {
                        const snippet = cluster.error_snippet.length > 120
                          ? cluster.error_snippet.slice(0, 120) + '…'
                          : cluster.error_snippet;
                        const dirCount = cluster.direction_ids.length;
                        const dirLabels = cluster.direction_ids
                          .map(id => dirTextById.get(id))
                          .filter((s): s is string => !!s)
                          .map(s => s.length > 30 ? s.slice(0, 30) + '…' : s);
                        return (
                          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                          <div
                            key={cluster.fingerprint}
                            role="button"
                            tabIndex={0}
                            className="flex flex-col gap-0.5 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                            onClick={() => onSelectTask({ id: cluster.first_task_id })}
                            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: cluster.first_task_id }); } }}
                          >
                            <div className="flex items-center gap-2">
                              <span className="flex-1 truncate text-neutral-400 font-mono text-[10px]">{snippet}</span>
                              <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                                ×{cluster.count}
                              </span>
                              <span
                                className="shrink-0 text-[10px] text-cyan-400 hover:text-cyan-300"
                                onClick={e => { e.stopPropagation(); onSelectTask({ id: cluster.first_task_id }); }}
                                title={`Open first task ${cluster.first_task_id}`}
                              >
                                view →
                              </span>
                            </div>
                            <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                              {dirCount > 1 && (
                                <span className="text-amber-400">across {dirCount} directions</span>
                              )}
                              {dirLabels.slice(0, 2).map((label) => (
                                <span key={label} className="text-[9px] text-neutral-500 truncate max-w-[180px]">
                                  · {label}
                                </span>
                              ))}
                              {cluster.task_types.map(tt => (
                                <span key={tt} className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                                  {tt}
                                </span>
                              ))}
                            </div>
                          </div>
                        );
                      });
                    })()}
                  </div>
                </div>
                );
              })()}

              {/* Early warnings — failure-rate momentum pre-anomaly signals */}
              {(digest.early_warnings ?? []).length > 0 && (() => {
                const earlyWarnings = digest.early_warnings ?? [];
                return (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-amber-400 uppercase tracking-widest">⚠ Early warnings</span>
                    <span className="text-[10px] text-neutral-400">
                      {earlyWarnings.length} rising trend{earlyWarnings.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1 ml-1">
                    {earlyWarnings.map((w, idx) => {
                      const progression = (w.rate_progression ?? [])
                        .map(r => `${Math.round(r * 100)}%`)
                        .join(' → ');
                      return (
                        <div
                          key={`${w.failure_class}-${idx}`}
                          className="flex flex-col gap-0.5 text-xs bg-amber-500/5 border border-amber-500/30 px-2 py-1.5 rounded"
                        >
                          <div className="flex items-center gap-2">
                            <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider bg-amber-500/10 text-amber-400 border-amber-500/20">
                              rising
                            </span>
                            <span className="flex-1 truncate text-amber-200 font-mono text-[10px]">{w.failure_class}</span>
                            {progression && (
                              <span className="shrink-0 text-[10px] font-semibold text-amber-300 font-mono">
                                {progression}
                              </span>
                            )}
                          </div>
                          <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                            {w.affected_direction_count > 0 && (
                              <span>across {w.affected_direction_count} direction{w.affected_direction_count !== 1 ? 's' : ''}</span>
                            )}
                            {w.severity && (
                              <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                                {w.severity}
                              </span>
                            )}
                            {w.suggested_action && (
                              <span className="text-neutral-400">→ {w.suggested_action}</span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
                );
              })()}

              {/* Recurring pattern detection — detection-to-fix lifecycle */}
              {(digest.recurring_patterns ?? []).length > 0 && (() => {
                const recurringPatterns = digest.recurring_patterns ?? [];
                return (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Recurring patterns</span>
                    <span className="text-[10px] text-neutral-400">
                      {recurringPatterns.length} pattern{recurringPatterns.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1 ml-1">
                    {recurringPatterns.map(pattern => {
                      const statusStyles: Record<string, string> = {
                        fix_verified: 'bg-green-500/10 text-green-400 border-green-500/20',
                        fix_shipped: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
                        investigating: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
                        unaddressed: 'bg-red-500/10 text-red-400 border-red-500/20',
                      };
                      const statusClass = statusStyles[pattern.remediation_status] ?? 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30';
                      const snippet = (pattern.example_message ?? '').length > 120
                        ? (pattern.example_message ?? '').slice(0, 120) + '…'
                        : (pattern.example_message ?? '');
                      return (
                        <div
                          key={pattern.fingerprint}
                          className="flex flex-col gap-0.5 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded"
                        >
                          <div className="flex items-center gap-2">
                            <span className={`shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider ${statusClass}`}>
                              {pattern.remediation_status.replace('_', ' ')}
                            </span>
                            <span className="flex-1 truncate text-neutral-400 font-mono text-[10px]">{snippet}</span>
                            <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-neutral-800/50 text-neutral-300 border border-neutral-700/30">
                              ×{pattern.occurrence_count}
                            </span>
                          </div>
                          <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                            {pattern.affected_direction_count > 0 && (
                              <span>across {pattern.affected_direction_count} direction{pattern.affected_direction_count !== 1 ? 's' : ''}</span>
                            )}
                            {pattern.severity && (
                              <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1 py-0.5 rounded uppercase tracking-wider">
                                {pattern.severity}
                              </span>
                            )}
                            {pattern.proactive && (
                              <span className="text-[9px] bg-purple-500/10 text-purple-400 border border-purple-500/20 px-1 py-0.5 rounded uppercase tracking-wider">
                                proactive
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
                );
              })()}

              {/* Review cap exhaustion summary */}
              {digest.review_cap_exhaustion_summary && digest.review_cap_exhaustion_summary.count > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Review cap exhaustion</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest.review_cap_exhaustion_summary.count} task{digest.review_cap_exhaustion_summary.count !== 1 ? 's' : ''} hit cap
                    </span>
                    {digest.review_cap_exhaustion_summary.avg_iterations != null && (
                      <span className="text-[10px] text-neutral-500">
                        · avg {digest.review_cap_exhaustion_summary.avg_iterations.toFixed(1)} iter
                      </span>
                    )}
                    {digest.review_cap_exhaustion_summary.total_cost_usd > 0 && (
                      <span className="text-[10px] text-red-400">
                        · ${digest.review_cap_exhaustion_summary.total_cost_usd.toFixed(2)} wasted
                      </span>
                    )}
                    {digest.review_cap_exhaustion_summary.avg_cost_usd != null && (
                      <span className="text-[10px] text-neutral-500">
                        · avg ${digest.review_cap_exhaustion_summary.avg_cost_usd.toFixed(2)}/task
                      </span>
                    )}
                  </div>
                  {digest.review_cap_exhaustion_summary.top_directions.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {(() => {
                        const dirTextById = new Map<string, string>(
                          (digest.direction_progress ?? []).map(dp => [dp.direction_id, dp.direction_text || dp.direction_id]),
                        );
                        return (digest.review_cap_exhaustion_summary?.top_directions ?? []).map(d => {
                          const text = dirTextById.get(d.direction_id) ?? d.direction_id;
                          const label = text.length > 60 ? text.slice(0, 60) + '…' : text;
                          return (
                            <span key={d.direction_id} className="text-[10px] text-neutral-500 truncate">
                              · {label}{' '}
                              <span className="text-red-400">×{d.count}</span>
                            </span>
                          );
                        });
                      })()}
                    </div>
                  )}
                </div>
              )}

              {/* Merge retries saved (early-exit efficiency) */}
              {digest.total_merge_retries_saved != null && digest.total_merge_retries_saved > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Early exit</span>
                  <span className="text-[10px] text-emerald-400 inline-flex items-center gap-0.5">
                    <CheckCircle className="w-2.5 h-2.5" />
                    {digest.total_merge_retries_saved} {digest.total_merge_retries_saved === 1 ? 'retry' : 'retries'} saved
                  </span>
                  {digest.merge_early_exit_count != null && digest.merge_early_exit_count > 0 && (
                    <span className="text-[10px] text-neutral-500">
                      · across {digest.merge_early_exit_count} task{digest.merge_early_exit_count === 1 ? '' : 's'}
                    </span>
                  )}
                </div>
              )}

              {/* Retry overhead cost */}
              {digest.retry_task_count != null && digest.retry_task_count > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry waste</span>
                  <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
                    <DollarSign className="w-2.5 h-2.5" />{(digest.retry_overhead_cost_usd ?? 0).toFixed(2)} wasted
                  </span>
                  <span className="text-[10px] text-neutral-400">
                    · {digest.retry_task_count} over-retried task{digest.retry_task_count === 1 ? '' : 's'}
                  </span>
                  {digest.avg_retries_before_failure != null && (
                    <span className="text-[10px] text-neutral-500">
                      · avg {digest.avg_retries_before_failure.toFixed(1)} attempts before failure
                    </span>
                  )}
                  {digest.retry_waste_by_direction && digest.retry_waste_by_direction.length > 0 && (
                    <ul className="w-full flex flex-col gap-0.5 mt-0.5 ml-2">
                      {digest.retry_waste_by_direction.map((d) => (
                        <li key={d.direction_id} className="text-[10px] text-neutral-400">
                          · <span className="text-neutral-300">{d.direction_text || d.direction_id}</span>
                          <span className="text-amber-400 ml-1">${d.retry_cost_usd.toFixed(2)}</span>
                          <span className="text-neutral-500 ml-1">({d.retry_task_count} task{d.retry_task_count === 1 ? '' : 's'})</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* Retry effectiveness */}
              {((digest.retry_success_count ?? 0) > 0 || digest.retry_wasted_rate != null) && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry effectiveness</span>
                  {(digest.retry_success_count ?? 0) > 0 && (
                    <span className="text-[10px] text-emerald-400">
                      {digest.retry_success_count} recovered
                    </span>
                  )}
                  {digest.retry_wasted_rate != null && (
                    <span className="text-[10px] text-amber-400">
                      · {Math.round(digest.retry_wasted_rate * 100)}% wasted
                    </span>
                  )}
                  {(digest.retry_recovery_cost_usd ?? 0) > 0 && (
                    <span className="text-[10px] text-neutral-500 inline-flex items-center gap-0.5">
                      · <DollarSign className="w-2.5 h-2.5" />{(digest.retry_recovery_cost_usd ?? 0).toFixed(2)} recovery cost
                    </span>
                  )}
                </div>
              )}

              {/* Resurrection outcomes */}
              {digest.resurrection_outcome_summary && digest.resurrection_outcome_summary.total > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Resurrection outcomes</span>
                  <span className="text-[10px] text-neutral-400">
                    {digest.resurrection_outcome_summary.total} attempted
                  </span>
                  <span className="text-[10px] text-emerald-400">
                    · {digest.resurrection_outcome_summary.completed} succeeded
                  </span>
                  <span className="text-[10px] text-amber-400">
                    · {digest.resurrection_outcome_summary.failed_again} failed again
                  </span>
                  {digest.resurrection_outcome_summary.success_rate != null && (
                    <span className="text-[10px] text-neutral-500">
                      · {Math.round(digest.resurrection_outcome_summary.success_rate * 100)}% success
                    </span>
                  )}
                </div>
              )}

              {/* Retry pattern summary */}
              {digest.retry_pattern_summary && digest.retry_pattern_summary.total_multi_attempt_failures > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry patterns</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest.retry_pattern_summary.total_multi_attempt_failures} multi-attempt
                    </span>
                    {digest.retry_pattern_summary.total_multi_attempt_failures > 0 && (
                      <span className="text-[10px] text-neutral-500">
                        · {Math.round((digest.retry_pattern_summary.repeat_class_count / digest.retry_pattern_summary.total_multi_attempt_failures) * 100)}% same class
                      </span>
                    )}
                  </div>
                  {digest.retry_pattern_summary.top_patterns.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {digest.retry_pattern_summary.top_patterns.slice(0, 3).map((p) => (
                        <span key={p.sequence} className="text-[10px] text-neutral-500 truncate font-mono">
                          · {p.sequence}{' '}
                          <span className="text-amber-400">×{p.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Analysis overflow summary */}
              {digest.analysis_overflow_summary && digest.analysis_overflow_summary.total_count > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Analysis overflow</span>
                  <span className="text-[10px] text-neutral-400">
                    {digest.analysis_overflow_summary.total_count} task{digest.analysis_overflow_summary.total_count === 1 ? '' : 's'} hit planning gate
                  </span>
                  {digest.analysis_overflow_summary.cost_usd > 0 && (
                    <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
                      <DollarSign className="w-2.5 h-2.5" />{digest.analysis_overflow_summary.cost_usd.toFixed(2)}
                    </span>
                  )}
                  {Object.entries(digest.analysis_overflow_summary.by_scope).map(([scope, count]) => (
                    <span key={scope} className="text-[10px] text-neutral-500">
                      · {scope} ×{count}
                    </span>
                  ))}
                </div>
              )}

              {/* Provision waste */}
              {digest.provision_waste_cost_usd != null && digest.provision_waste_cost_usd > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Provision waste</span>
                    <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
                      <DollarSign className="w-2.5 h-2.5" />{digest.provision_waste_cost_usd.toFixed(2)} burned
                    </span>
                    {digest.provisioning_overhead_pct != null && (
                      <span className="text-[10px] text-neutral-500">
                        · {(digest.provisioning_overhead_pct * 100).toFixed(1)}% of total compute
                      </span>
                    )}
                  </div>
                  {digest.provision_waste_by_direction && digest.provision_waste_by_direction.length > 0 && (
                    <ul className="w-full flex flex-col gap-0.5 mt-0.5 ml-2">
                      {digest.provision_waste_by_direction.map((d) => (
                        <li key={d.direction_id} className="text-[10px] text-neutral-400">
                          · <span className="text-neutral-300">{d.direction_text || d.direction_id}</span>
                          <span className="text-amber-400 ml-1">${d.provision_cost_usd.toFixed(2)}</span>
                          <span className="text-neutral-500 ml-1">({d.provision_task_count} task{d.provision_task_count === 1 ? '' : 's'})</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* Provisioning overhead (standalone if no waste block) */}
              {(digest.provision_waste_cost_usd == null || digest.provision_waste_cost_usd === 0) &&
                digest.provisioning_overhead_pct != null && digest.provisioning_overhead_pct > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Provisioning overhead</span>
                  <span className="text-[10px] text-neutral-400">
                    {(digest.provisioning_overhead_pct * 100).toFixed(1)}% of total compute
                  </span>
                </div>
              )}

              {/* Escalation outcomes */}
              {digest.escalation_outcome_summary && digest.escalation_outcome_summary.total_attempted > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Escalation outcomes</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest.escalation_outcome_summary.total_attempted} attempted
                    </span>
                    <span className="text-[10px] text-emerald-400">
                      · {digest.escalation_outcome_summary.total_merged} merged
                    </span>
                    <span className="text-[10px] text-red-400">
                      · {digest.escalation_outcome_summary.total_failed} failed
                    </span>
                    {digest.escalation_outcome_summary.overall_success_rate != null && (
                      <span className="text-[10px] text-neutral-500">
                        · {Math.round(digest.escalation_outcome_summary.overall_success_rate * 100)}% success
                      </span>
                    )}
                  </div>
                  {Object.entries(digest.escalation_outcome_summary.by_path).length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {Object.entries(digest.escalation_outcome_summary.by_path).map(([path, b]) => (
                        <span key={path} className="text-[10px] text-neutral-500 truncate font-mono">
                          · {path} <span className="text-neutral-400">{b.merged}/{b.attempted}</span>
                          {b.success_rate != null && (
                            <span className="text-neutral-500"> ({Math.round(b.success_rate * 100)}%)</span>
                          )}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Retry success rate by class */}
              {digest.retry_success_rate_by_class && Object.keys(digest.retry_success_rate_by_class).length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry success by class</span>
                  </div>
                  <div className="flex flex-col gap-0.5 ml-1">
                    {Object.entries(digest.retry_success_rate_by_class)
                      .sort((a, b) => (b[1].merged + b[1].failed) - (a[1].merged + a[1].failed))
                      .map(([cls, b]) => (
                        <span key={cls} className="text-[10px] text-neutral-500 truncate font-mono">
                          · {cls} <span className="text-emerald-400">{b.merged}</span>
                          <span className="text-neutral-500">/</span>
                          <span className="text-red-400">{b.failed}</span>
                          <span className="text-neutral-400"> ({Math.round(b.success_rate * 100)}%)</span>
                        </span>
                      ))}
                  </div>
                </div>
              )}

              {/* Review quality */}
              {(() => {
                const sre = digest.self_review_effectiveness;
                const spa = digest.self_review_prediction_accuracy;
                const spaTrends = digest.self_review_prediction_accuracy_trends;
                const ra = digest.review_accuracy;
                const fra = digest.first_review_approval_rate;
                const skip = digest.self_review_skip_rate;
                const fre = digest.fresh_reviewer_effectiveness;
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
                      onClick={() => setReviewQualityExpanded(v => !v)}
                      aria-expanded={reviewQualityExpanded}
                      className="flex items-center gap-1.5 text-[10px] font-bold text-neutral-500 uppercase tracking-widest hover:text-neutral-400 transition-colors"
                    >
                      {reviewQualityExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                      Review quality
                    </button>
                    {reviewQualityExpanded && (
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
              })()}


              {/* Recent failures grouped by classification (not in needs_attention) */}
              {extraFailureGroups.length > 0 && (() => {
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
                return (
                <div>
                  <button
                    onClick={() => setFailuresExpanded(v => !v)}
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
                        const classHistory = digest.trend_history ?? [];
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
                              {digest.cost_by_failure_class?.[group.failure_class] != null && digest.cost_by_failure_class[group.failure_class] > 0 && (
                                <span className="text-[10px] text-red-400/70 font-mono" title={`Cost of ${group.label} failures`}>
                                  ${digest.cost_by_failure_class[group.failure_class].toFixed(2)}
                                </span>
                              )}
                            </button>
                            {isGroupExpanded && (
                              <div className="flex flex-col gap-1 ml-5 max-w-full">
                                {group.tasks.map(t => (
                                  <div key={t.task_id} className="flex flex-col gap-0.5">
                                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                                    <div
                                      role="button"
                                      tabIndex={0}
                                      className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
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
                                          onClick={() => toggleSnippet(t.task_id)}
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
              })()}

              {/* Remediation pipeline */}
              {digest.remediation_funnel && (
                digest.remediation_funnel.investigations_spawned > 0
                || (digest.remediation_funnel.ci_auto_remediation?.spawned ?? 0) > 0
                || Object.keys(digest.remediation_funnel.cumulative_merged_by_trigger_class ?? {}).length > 0
              ) && (() => {
                const rf = digest.remediation_funnel;
                if (!rf) return null;
                const successRate = rf.remediations_spawned > 0
                  ? rf.remediations_merged / rf.remediations_spawned
                  : null;
                const stages: { label: string; value: number }[] = [
                  { label: 'Spawned', value: rf.investigations_spawned },
                  { label: 'Completed', value: rf.investigations_completed },
                  { label: 'Diagnosed', value: rf.investigations_with_diagnosis },
                  { label: 'Remediations', value: rf.remediations_spawned },
                  { label: 'Merged', value: rf.remediations_merged },
                  ...(rf.fixes_held != null ? [{ label: 'Held', value: rf.fixes_held }] : []),
                ];
                const maxVal = Math.max(1, ...stages.map(s => s.value));
                return (
                  <div>
                    <button
                      onClick={() => setRemediationExpanded(v => !v)}
                      aria-expanded={remediationExpanded}
                      className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                    >
                      {remediationExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                      <RotateCcw className="w-3 h-3 text-cyan-500" />
                      Remediation pipeline ({rf.investigations_spawned})
                    </button>
                    {remediationExpanded && (
                      <div className="flex flex-col gap-2">
                        <div className="flex items-center gap-3 text-[10px] text-neutral-400">
                          {successRate != null && (
                            <span>
                              <span className="text-neutral-500">success:</span>{' '}
                              <span className="text-emerald-400 font-mono">{(successRate * 100).toFixed(0)}%</span>
                              <span className="text-neutral-600"> ({rf.remediations_merged}/{rf.remediations_spawned})</span>
                            </span>
                          )}
                          {rf.avg_time_to_remediation_hours != null && (
                            <span>
                              <span className="text-neutral-500">avg time:</span>{' '}
                              <span className="text-neutral-300 font-mono">{rf.avg_time_to_remediation_hours.toFixed(1)}h</span>
                            </span>
                          )}
                          {rf.active_investigations > 0 && (
                            <span>
                              <span className="text-neutral-500">active:</span>{' '}
                              <span className="text-cyan-400 font-mono">{rf.active_investigations}</span>
                            </span>
                          )}
                          {rf.remediations_failed > 0 && (
                            <span>
                              <span className="text-neutral-500">failed:</span>{' '}
                              <span className="text-red-400 font-mono">{rf.remediations_failed}</span>
                            </span>
                          )}
                          {rf.fixes_held != null && rf.fixes_held > 0 && (
                            <span>
                              <span className="text-neutral-500">held:</span>{' '}
                              <span className="text-emerald-400 font-mono">{rf.fixes_held}</span>
                            </span>
                          )}
                        </div>
                        <div className="flex flex-col gap-1">
                          {stages.map((s, i) => {
                            const pct = (s.value / maxVal) * 100;
                            return (
                              <div key={s.label} className="flex items-center gap-2 text-[10px]">
                                <span className="w-24 text-neutral-500 shrink-0">
                                  {i > 0 && <span className="text-neutral-700">↓ </span>}
                                  {s.label}
                                </span>
                                <div className="flex-1 bg-neutral-900 rounded h-4 relative overflow-hidden">
                                  <div
                                    className="absolute inset-y-0 left-0 bg-cyan-500/30 border-r border-cyan-500/50"
                                    style={{ width: `${pct}%` }}
                                  />
                                  <span className="absolute inset-0 flex items-center px-1.5 font-mono text-neutral-300">
                                    {s.value}
                                  </span>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                        {(() => {
                          const confOrder = ['high', 'medium', 'low'];
                          const confColor: Record<string, string> = { high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-neutral-400' };
                          const ibc = rf.investigations_by_confidence ?? {};
                          const rsbc = rf.remediation_success_by_confidence ?? {};
                          const frbc = rf.failed_remediations_by_class ?? {};
                          const mrbc = rf.merged_remediations_by_trigger_class ?? {};
                          const cmbc = rf.cumulative_merged_by_trigger_class ?? {};
                          const car = rf.ci_auto_remediation;
                          const dropOff = rf.diagnosis_drop_off_by_reason ?? {};
                          const ibcEntries = confOrder.filter(k => (ibc[k] ?? 0) > 0);
                          const rsbcEntries = confOrder.filter(k => { const v = rsbc[k]; return !!v && (v.merged + v.failed) > 0; });
                          const sortDesc = <T,>(entries: [string, T][], key: (v: T) => number) => entries.sort((a, b) => key(b[1]) - key(a[1]));
                          const frbcEntries = sortDesc(Object.entries(frbc).filter(([, v]) => v > 0), v => v);
                          const mrbcEntries = sortDesc(Object.entries(mrbc).filter(([, v]) => v > 0), v => v);
                          const cmbcEntries = sortDesc(Object.entries(cmbc).filter(([, v]) => v > 0), v => v);
                          const carTypeEntries = sortDesc(Object.entries(car?.by_ci_failure_type ?? {}).filter(([, v]) => (v.spawned + v.merged + v.failed) > 0), v => v.spawned);
                          const dropOffEntries = sortDesc(Object.entries(dropOff).filter(([, v]) => v > 0), v => v);
                          const hasCar = !!car && (car.spawned + car.merged + car.failed) > 0;
                          const dropOffTotal = dropOffEntries.reduce((acc, [, v]) => acc + v, 0);
                          const dropOffDominant = dropOffEntries[0]?.[0];
                          type Row = { key: string; label: string; nodes: React.ReactNode };
                          const rows: Row[] = [];
                          if (ibcEntries.length) rows.push({ key: 'ibc', label: 'confidence:', nodes: ibcEntries.map(k => (
                            <span key={k} className="inline-flex items-baseline gap-1"><span className={confColor[k]}>{k}</span><span className="font-mono text-neutral-300">{ibc[k]}</span></span>
                          )) });
                          if (rsbcEntries.length) rows.push({ key: 'rsbc', label: 'success/conf:', nodes: rsbcEntries.map(k => {
                            const v = rsbc[k]; if (!v) return null;
                            const total = v.merged + v.failed; const pct = total > 0 ? Math.round((v.merged / total) * 100) : 0;
                            return <span key={k} className="inline-flex items-baseline gap-1"><span className={confColor[k]}>{k}</span><span className="font-mono text-neutral-300">{pct}%</span><span className="text-neutral-600">({v.merged}/{total})</span></span>;
                          }) });
                          if (frbcEntries.length) rows.push({ key: 'frbc', label: 'failed class:', nodes: frbcEntries.map(([cls, count]) => (
                            <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-red-400">{cls}</span><span className="font-mono text-neutral-300">{count}</span></span>
                          )) });
                          if (mrbcEntries.length) rows.push({ key: 'mrbc', label: 'merged class:', nodes: mrbcEntries.map(([cls, count]) => (
                            <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-emerald-400">{cls}</span><span className="font-mono text-neutral-300">{count}</span></span>
                          )) });
                          if (cmbcEntries.length) rows.push({ key: 'cmbc', label: 'all-time merged by class:', nodes: cmbcEntries.map(([cls, count]) => (
                            <span key={cls} className="inline-flex items-baseline gap-1"><span className={count >= 3 ? 'text-emerald-400' : 'text-amber-400'}>{cls}</span><span className="font-mono text-neutral-300">{count}</span>{count >= 3 && <span className="text-emerald-600 text-[9px]">✓</span>}</span>
                          )) });
                          if (hasCar && car) rows.push({ key: 'car', label: 'ci auto-remediation:', nodes: (['spawned', 'merged', 'failed'] as const).map(k => (
                            <span key={k} className="inline-flex items-baseline gap-1"><span className="text-neutral-500">{k}</span><span className={`font-mono ${k === 'merged' ? 'text-emerald-400' : k === 'failed' ? 'text-red-400' : 'text-neutral-300'}`}>{car[k]}</span></span>
                          )) });
                          if (dropOffEntries.length) rows.push({ key: 'drop', label: 'diagnosis drop-off:', nodes: dropOffEntries.map(([reason, count]) => (
                            <span key={reason} className="inline-flex items-baseline gap-1"><span className={reason === dropOffDominant ? 'text-amber-400' : 'text-neutral-400'}>{reason}</span><span className="font-mono text-neutral-300">{count}</span>{dropOffTotal > 0 && <span className="text-neutral-600">({Math.round((count / dropOffTotal) * 100)}%)</span>}</span>
                          )) });
                          if (carTypeEntries.length) rows.push({ key: 'carT', label: 'ci by failure type:', nodes: carTypeEntries.map(([cls, v]) => (
                            <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-cyan-400">{cls}</span><span className="font-mono text-neutral-300">{v.spawned}</span><span className="text-neutral-600">({v.merged}✓/{v.failed}✗)</span></span>
                          )) });
                          if (!rows.length) return null;
                          return (
                            <div className="flex flex-col gap-1 text-[10px] text-neutral-400 border-t border-neutral-800 pt-1.5">
                              {rows.map(r => (
                                <div key={r.key} className="flex items-center gap-2 flex-wrap">
                                  <span className="text-neutral-500 w-24 shrink-0">{r.label}</span>
                                  {r.nodes}
                                </div>
                              ))}
                            </div>
                          );
                        })()}
                        {(() => {
                          const recent = rf.recent_investigations ?? [];
                          const active = rf.active_investigations_detail ?? [];
                          if (!recent.length && !active.length) return null;
                          const visibleRecent = investigationsExpanded || recent.length <= 2
                            ? recent
                            : recent.slice(0, 2);
                          const confBadge: Record<string, string> = {
                            high: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
                            medium: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
                            low: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
                          };
                          const sevBadge: Record<string, string> = {
                            high: 'bg-red-500/20 text-red-400 border-red-500/30',
                            medium: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
                            low: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
                          };
                          return (
                            <div className="flex flex-col gap-1.5 text-[10px] border-t border-neutral-800 pt-1.5">
                              {recent.length > 0 && (
                                <div className="flex flex-col gap-1">
                                  <div className="text-neutral-500">recent diagnoses:</div>
                                  {visibleRecent.map(inv => (
                                    <div key={inv.task_id} className="flex items-start gap-2">
                                      <span className="flex-1 text-neutral-300 truncate">
                                        {inv.summary || <span className="text-neutral-600 italic">(no summary)</span>}
                                      </span>
                                      {inv.confidence && (
                                        <span className={`${confBadge[inv.confidence] ?? confBadge.low} border px-1 rounded uppercase text-[9px] font-semibold shrink-0`}>
                                          {inv.confidence}
                                        </span>
                                      )}
                                      {inv.severity && (
                                        <span className={`${sevBadge[inv.severity] ?? sevBadge.low} border px-1 rounded uppercase text-[9px] font-semibold shrink-0`}>
                                          {inv.severity}
                                        </span>
                                      )}
                                      {inv.completed_at && <span className="text-neutral-600 font-mono shrink-0">{timeAgo(inv.completed_at)}</span>}
                                    </div>
                                  ))}
                                  {recent.length > 2 && (
                                    <button
                                      onClick={() => setInvestigationsExpanded(v => !v)}
                                      className="text-[9px] text-neutral-500 hover:text-neutral-300 self-start"
                                    >
                                      {investigationsExpanded ? 'show less' : `show ${recent.length - 2} more`}
                                    </button>
                                  )}
                                </div>
                              )}
                              {active.length > 0 && (
                                <div className="flex flex-col gap-1">
                                  <div className="text-neutral-500">active investigations:</div>
                                  {active.map(inv => {
                                    const wedged = (inv.elapsed_hours ?? 0) > 4;
                                    return (
                                      <div key={inv.task_id} className={`flex items-center gap-2 ${wedged ? 'text-amber-400' : 'text-neutral-400'}`}>
                                        <span className="font-mono shrink-0">
                                          {inv.elapsed_hours != null ? `${inv.elapsed_hours.toFixed(1)}h` : '—'}
                                        </span>
                                        <span className="truncate text-neutral-500">
                                          dir: {inv.direction_id ?? '—'}
                                        </span>
                                        {wedged && <span className="text-[9px] uppercase font-semibold shrink-0">wedged?</span>}
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })()}
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* Recent completions */}
              {(digest.recent_completions ?? []).length > 0 && (() => {
                const completions = digest.recent_completions ?? [];
                // Group by task_type, preserving insertion order
                const groups = new Map<string, RecentCompletion[]>();
                for (const c of completions) {
                  const key = c.task_type || 'other';
                  let bucket = groups.get(key);
                  if (!bucket) { bucket = []; groups.set(key, bucket); }
                  bucket.push(c);
                }
                const groupEntries = Array.from(groups.entries());
                const allSameType = groupEntries.length === 1;

                return (
                  <div>
                    <button
                      onClick={() => setCompletionsExpanded(v => !v)}
                      aria-expanded={completionsExpanded}
                      className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                    >
                      {completionsExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                      <CheckCircle className="w-3 h-3 text-emerald-500" />
                      Recent completions ({completions.length})
                    </button>
                    {completionsExpanded && (
                      <div className="flex flex-col gap-2">
                        {/* Type summary line */}
                        <div className="flex flex-wrap items-center gap-1.5">
                          {groupEntries.map(([type, items], idx) => {
                            const style = TASK_TYPE_STYLES[type];
                            return (
                              <span key={type} className="flex items-center gap-1 text-[10px]">
                                {idx > 0 && <span className="text-neutral-700">·</span>}
                                <span className={`${style?.bg ?? 'bg-neutral-500/10 border-neutral-500/20'} ${style?.text ?? 'text-neutral-400'} border px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider`}>
                                  {items.length} {style?.label ?? type}
                                </span>
                              </span>
                            );
                          })}
                        </div>
                        {/* Grouped completion rows */}
                        {allSameType ? (
                          <div className="flex flex-col gap-1.5">
                            {completions.map(c => (
                              <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
                            ))}
                          </div>
                        ) : (
                          groupEntries.map(([type, items]) => (
                            <div key={type} className="flex flex-col gap-1.5">
                              <div className="flex items-center gap-1.5">
                                <TaskTypeBadge taskType={type as TaskType} />
                              </div>
                              {items.map(c => (
                                <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
                              ))}
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
