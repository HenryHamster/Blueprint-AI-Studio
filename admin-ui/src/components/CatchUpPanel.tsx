import { useState, useEffect, useCallback, useRef } from 'react';
import type { RefObject } from 'react';
import { Clock, ChevronDown, ChevronUp, AlertTriangle, CheckCircle, XCircle, Loader2, Check, RotateCcw, DollarSign, Compass } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';
import { isSafeUrl, TaskTypeBadge, TASK_TYPE_STYLES, FAILURE_CLASS_CONFIG } from './TaskCard';
import type { TaskType } from './TaskCard';

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

const TIMEOUT_SUBTYPE_LABELS: Record<string, string> = {
  heartbeat_lost: 'heartbeat lost',
  oom: 'OOM',
  execution_timeout: 'exec timeout',
  cascade_cancellation: 'cascade',
  iteration_limit: 'iter limit',
  process_crash: 'crash',
  unknown: 'unknown',
};

interface FailureGroup {
  failure_class: string;
  label: string;
  count: number;
  tasks: FailureGroupTask[];
}

interface RecentCompletion {
  task_id: string;
  title: string;
  completed_at: string | null;
  pr_url: string | null;
  task_type: string;
  direction_id?: string | null;
  direction_text?: string | null;
  scope?: string | null;
  duration_seconds?: number | null;
  review_iterations?: number | null;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  return hours < 10 ? `${hours.toFixed(1)}h` : `${Math.round(hours)}h`;
}

interface DirectionProgress {
  direction_id: string;
  direction_text: string;
  priority: string;
  cycles_in_window: number;
  tasks_created: number;
  prs_merged: number;
  possibly_saturated: boolean;
  latest_insights?: string[];
  recent_finding_titles?: string[];
  latest_research_at?: string | null;
  prs_rejected?: number;
  acceptance_rate?: number | null;
  first_attempt_success_rate?: number | null;
  cost_per_merged_pr?: number | null;
  auto_paused?: boolean;
  paused_reason?: string | null;
  needs_manual_intervention?: boolean;
  scope_breakdown?: Record<string, { created?: number; merged?: number; failed?: number }> | null;
  taxonomy_summary?: Record<string, number> | null;
  stage_dropout?: Record<string, number> | null;
  rejection_reasons?: { task_id?: string; title?: string; rejection_note?: string }[] | null;
  lifetime_cycles?: number;
  lifetime_prs_merged?: number;
  lifetime_prs_rejected?: number;
  lifetime_tasks_failed?: number;
  lifetime_conversion_rate?: number | null;
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
  trend_history?: {period_end: string; success_rate: number | null; completed: number; failed: number; failed_by_taxonomy?: Record<string, number>}[];
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
  total_estimated_cost_usd?: number | null;
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
  review_cap_exhaustion_summary?: {
    count: number;
    avg_iterations: number | null;
    top_directions: { direction_id: string; count: number }[];
  } | null;
}

const TAXONOMY_ORDER = ['scope_related', 'review_quality', 'infra_related', 'code_defect'] as const;
const TAXONOMY_LABELS: Record<string, string> = {
  scope_related: 'scope',
  review_quality: 'review',
  infra_related: 'infra',
  code_defect: 'code',
};
const TAXONOMY_COLORS: Record<string, string> = {
  scope_related: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review_quality: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  infra_related: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  code_defect: 'bg-red-500/20 text-red-400 border-red-500/30',
};

const STAGE_ORDER = ['provision', 'clone', 'test', 'lint', 'build', 'pr_creation', 'merge', 'review'] as const;
const STAGE_COLORS: Record<string, string> = {
  provision: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  clone: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  test: 'bg-red-500/20 text-red-400 border-red-500/30',
  lint: 'bg-red-500/20 text-red-400 border-red-500/30',
  build: 'bg-red-500/20 text-red-400 border-red-500/30',
  pr_creation: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  merge: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
};

export interface CatchUpPanelProps {
  selectedProjectId: string | null;
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

type TimeWindow = 'last_visit' | '6h' | '24h' | '7d';

type DismissEntry = { type: 'dismiss' } | { type: 'snooze'; until: number };
type DismissedState = Record<string, DismissEntry>;

function loadDismissed(): DismissedState {
  try {
    const raw = localStorage.getItem('blueprint-admin:dismissedAttention');
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveDismissed(state: DismissedState): void {
  localStorage.setItem('blueprint-admin:dismissedAttention', JSON.stringify(state));
}

function formatSnoozeRemaining(until: number): string {
  const remaining = until - Date.now();
  if (remaining <= 0) return 'expired';
  const hours = Math.floor(remaining / (3600 * 1000));
  const mins = Math.floor((remaining % (3600 * 1000)) / 60000);
  if (hours > 0) return `snoozed for ${hours}h more`;
  return `snoozed for ${mins}m more`;
}

const CATCH_UP_VIEWED_KEY = 'blueprint-admin:lastCatchUpViewed';

function getLastCatchUpViewed(): number | null {
  try {
    const stored = localStorage.getItem(CATCH_UP_VIEWED_KEY);
    const ts = stored ? parseInt(stored, 10) : null;
    return ts && !isNaN(ts) ? ts : null;
  } catch { return null; }
}

function markCatchUpViewed() {
  try { localStorage.setItem(CATCH_UP_VIEWED_KEY, String(Date.now())); } catch { /* */ }
}

function windowToHours(window: TimeWindow, lastViewed: number | null): number {
  switch (window) {
    case 'last_visit': {
      if (!lastViewed) return 24;
      const elapsed = (Date.now() - lastViewed) / (1000 * 60 * 60);
      return Math.max(1, Math.min(168, Math.ceil(elapsed)));
    }
    case '6h': return 6;
    case '24h': return 24;
    case '7d': return 168;
  }
}

function WaitingBadge({ hours }: { hours: number | undefined }) {
  if (hours === undefined || hours === null) return null;
  const label = hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
  const color = hours > 24
    ? 'bg-red-500/15 text-red-400 border-red-500/20'
    : hours >= 6
      ? 'bg-amber-500/15 text-amber-400 border-amber-500/20'
      : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30';
  return (
    <span className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${color}`}>
      {label}
    </span>
  );
}

function CompletionRow({ c, onSelectTask }: { c: RecentCompletion; onSelectTask: (task: { id: string }) => void }) {
  const prNum = c.pr_url?.match(/\/(\d+)\/?$/)?.[1];
  return (
    <div
      role="button"
      tabIndex={0}
      className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
      onClick={() => onSelectTask({ id: c.task_id })}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: c.task_id }); } }}
    >
      <span className="flex-1 truncate text-neutral-400">{c.title}</span>
      <div className="shrink-0 flex items-center gap-2">
        {c.direction_text && (
          <span
            className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[12rem]"
            title={c.direction_text}
          >
            {c.direction_text.length > 40 ? c.direction_text.slice(0, 40) + '…' : c.direction_text}
          </span>
        )}
        {c.scope && (
          <span
            className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
            title={`scope: ${c.scope}`}
          >
            {c.scope}
          </span>
        )}
        {typeof c.duration_seconds === 'number' && c.duration_seconds > 0 && (
          <span className="text-[9px] text-neutral-500 font-mono" title="duration">
            {formatDuration(c.duration_seconds)}
          </span>
        )}
        {typeof c.review_iterations === 'number' && c.review_iterations > 1 && (
          <span
            className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20"
            title="review iterations"
          >
            {c.review_iterations} cycles
          </span>
        )}
        {c.pr_url && isSafeUrl(c.pr_url) && (
          <a
            href={c.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-[9px] text-emerald-400 hover:text-emerald-300 font-medium"
          >
            {prNum ? `PR #${prNum}` : 'PR'}
          </a>
        )}
        {c.completed_at && (
          <span className="text-[10px] text-neutral-600 font-mono">{timeAgo(c.completed_at)}</span>
        )}
      </div>
    </div>
  );
}

function SkeletonRow() {
  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-neutral-950 border border-neutral-800/60 animate-pulse">
      <div className="h-2.5 bg-neutral-700/50 rounded flex-1" />
      <div className="h-2.5 bg-neutral-700/50 rounded w-12" />
    </div>
  );
}

function DirectionProgressSection({ directions }: { directions: DirectionProgress[] }) {
  const [expandedDirections, setExpandedDirections] = useState<Set<string>>(new Set());

  const toggleDirection = (id: string) => {
    setExpandedDirections(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="flex flex-col gap-1">
      <div className="text-[11px] font-bold text-neutral-500 uppercase tracking-widest mb-0.5">Directions</div>
      {directions.map(dp => {
        const isExpanded = expandedDirections.has(dp.direction_id);
        return (
          <div key={dp.direction_id}>
            <button
              className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/30 w-full text-left"
              onClick={() => toggleDirection(dp.direction_id)}
              aria-expanded={isExpanded}
            >
              {isExpanded ? <ChevronUp className="w-3 h-3 shrink-0 text-neutral-600" /> : <ChevronDown className="w-3 h-3 shrink-0 text-neutral-600" />}
              <span className="flex-1 truncate text-neutral-400">
                {(dp.direction_text || dp.direction_id).slice(0, 50)}
              </span>
              <div className="shrink-0 flex items-center gap-1">
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
                {typeof dp.cost_per_merged_pr === 'number' && dp.cost_per_merged_pr > 0 && (
                  <span className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded font-mono" title="Average cost per merged PR">
                    ${dp.cost_per_merged_pr.toFixed(2)}/PR
                  </span>
                )}
              </div>
            </button>
            {isExpanded && (
              <div className="mt-1 ml-5 flex flex-col gap-1">
                {dp.recent_finding_titles && dp.recent_finding_titles.length > 0 && (
                  <div className="flex flex-col gap-0.5">
                    {dp.recent_finding_titles.slice(0, 3).map((title, i) => (
                      <span key={i} className="text-[10px] text-neutral-400">· {title}</span>
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
                      if (!created && !merged) return null;
                      return (
                        <span key={scope} className="text-[9px] bg-neutral-700/30 text-neutral-400 border border-neutral-700/30 px-1.5 py-0.5 rounded">
                          {scope} {merged}/{created}
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

export function CatchUpPanel({ selectedProjectId, lastVisitTimestamp: _lastVisitTimestamp, refreshInterval, onSelectTask, needsAttentionCount = 0, onApprove, onRetry, onBulkApprove, onBulkRetry, wsRef }: CatchUpPanelProps) {
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

  const isHidden = (taskId: string): boolean => {
    const entry = dismissed[taskId];
    if (!entry) return false;
    if (entry.type === 'dismiss') return true;
    if (entry.type === 'snooze') return Date.now() < entry.until;
    return false;
  };

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

  return (
    <div className="mb-4 shrink-0">
      <div className="flex items-center gap-2">
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
          <div className="flex items-center gap-1 ml-2">
            {windowLabels.map(({ key, label }) => (
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
        <div className="text-[10px] text-neutral-500 mt-0.5 pl-7 truncate max-w-xl">
          {digest.narrative_summary.slice(0, 100)}{digest.narrative_summary.length > 100 ? '…' : ''}
        </div>
      )}

      {!collapsed && (
        <div className="mt-2 bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4">
          {loading ? (
            <div className="flex flex-col gap-2">
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
            </div>
          ) : !hasActivity ? (
            <div className="flex items-center gap-2 text-xs text-neutral-600 py-1">
              <CheckCircle className="w-3.5 h-3.5" />
              All clear — nothing to catch up on
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {(digest!.narrative_summary || digest!.trend) && (
                <div className="flex items-start gap-2 mb-3">
                  {digest!.narrative_summary && (
                    <p className="text-sm text-neutral-300 leading-relaxed flex-1">
                      {activeWindow === 'last_visit' && digest!.window_hours && (
                        <span className="text-neutral-400">Since you last checked ({digest!.window_hours}h ago): </span>
                      )}
                      {digest!.narrative_summary}
                    </p>
                  )}
                  {digest!.trend && (
                    <span
                      className={`shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full border ${
                        digest!.trend === 'improving'
                          ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20'
                          : digest!.trend === 'declining'
                            ? 'bg-red-500/15 text-red-400 border-red-500/20'
                            : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30'
                      }`}
                      aria-label={`Trend: ${digest!.trend === 'improving' ? 'Improving' : digest!.trend === 'declining' ? 'Declining' : 'Stable'}`}
                    >
                      {digest!.trend === 'improving' ? 'Improving' : digest!.trend === 'declining' ? 'Declining' : 'Stable'}
                    </span>
                  )}
                </div>
              )}
              {/* Stats row */}
              <div className="flex flex-wrap gap-3 text-[11px] text-neutral-400">
                {digest!.success_rate != null && (
                  <span
                    className={`flex items-center gap-1 ${digest!.success_rate >= 0.8 ? 'text-emerald-400' : digest!.success_rate >= 0.5 ? 'text-amber-400' : 'text-red-400'}`}
                    aria-label={`Success rate: ${Math.round(digest!.success_rate * 100)}% (${digest!.success_rate >= 0.8 ? 'healthy' : digest!.success_rate >= 0.5 ? 'warning' : 'critical'})`}
                  >
                    {Math.round(digest!.success_rate * 100)}% success
                    {digest!.success_rate_delta != null && (
                      <span className={digest!.success_rate_delta > 0 ? 'text-emerald-400' : digest!.success_rate_delta < 0 ? 'text-red-400' : 'text-neutral-500'}>
                        {digest!.success_rate_delta > 0 ? '+' : ''}{Math.round(digest!.success_rate_delta * 100)}%
                      </span>
                    )}
                    {digest!.trend_history && digest!.trend_history.length >= 3 && (
                      <div className="flex items-end gap-px h-3 ml-1">
                        {digest!.trend_history.map((p, i) => {
                          const rate = p.success_rate ?? 0;
                          const color = rate >= 0.8 ? 'bg-emerald-400' : rate >= 0.5 ? 'bg-amber-400' : 'bg-red-400';
                          const dateLabel = (() => {
                            const d = new Date(p.period_end);
                            return isNaN(d.getTime()) ? p.period_end : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
                          })();
                          const ratePart = p.success_rate == null ? 'no data' : `${Math.round(p.success_rate * 100)}%`;
                          const title = `${dateLabel}: ${ratePart} (${p.completed} done, ${p.failed} failed)`;
                          return <div key={i} title={title} className={`w-1 rounded-sm ${p.success_rate == null ? 'bg-neutral-700' : color}`} style={{height: `${Math.max(rate * 100, 8)}%`}} />;
                        })}
                      </div>
                    )}
                  </span>
                )}
                {digest!.pending_approval_count > 0 && (
                  <span className="flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3 text-amber-400" />
                    {digest!.pending_approval_count} pending approval
                  </span>
                )}
                {digest!.completed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <CheckCircle className="w-3 h-3 text-emerald-500" />
                    {digest!.completed_count} completed
                    {digest!.completed_delta != null && digest!.completed_delta !== 0 && (
                      <span
                        className={digest!.completed_delta > 0 ? 'text-emerald-400' : 'text-red-400'}
                        title={digest!.prev_completed_count != null ? `was ${digest!.prev_completed_count} last period` : undefined}
                      >
                        {digest!.completed_delta > 0 ? '↑' : '↓'}
                      </span>
                    )}
                  </span>
                )}
                {digest!.failed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <XCircle className="w-3 h-3 text-red-500" />
                    {digest!.failed_count} failed
                    {digest!.failed_delta != null && digest!.failed_delta !== 0 && (
                      <span
                        className={digest!.failed_delta > 0 ? 'text-red-400' : 'text-emerald-400'}
                        title={digest!.prev_failed_count != null ? `was ${digest!.prev_failed_count} last period` : undefined}
                      >
                        {digest!.failed_delta > 0 ? '↑' : '↓'}
                      </span>
                    )}
                    {digest!.total_failed_cost_usd != null && digest!.total_failed_cost_usd > 0 && (
                      <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono">
                        ·<DollarSign className="w-2.5 h-2.5" />{digest!.total_failed_cost_usd.toFixed(2)} wasted
                      </span>
                    )}
                  </span>
                )}
                {digest!.total_estimated_cost_usd != null && digest!.total_estimated_cost_usd > 0 && (
                  <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono" title="Total estimated spend">
                    <DollarSign className="w-2.5 h-2.5" />{digest!.total_estimated_cost_usd.toFixed(2)} total
                  </span>
                )}
                {digest!.daily_cost_usd != null && digest!.daily_cost_usd > 0 && (() => {
                  const spent = digest!.daily_cost_usd!;
                  const ceiling = digest!.daily_cost_ceiling_usd ?? 0;
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
                {digest!.research_cycles > 0 && (
                  <span>{digest!.research_cycles} research cycle{digest!.research_cycles !== 1 ? 's' : ''}</span>
                )}
                {digest!.prs_opened > 0 && (
                  <span>{digest!.prs_opened} PR{digest!.prs_opened !== 1 ? 's' : ''} opened</span>
                )}
                {digest!.running_count > 0 && (
                  <span className="flex items-center gap-1">
                    <Loader2 className="w-3 h-3 text-neutral-500 animate-spin" />
                    {digest!.running_count} running
                  </span>
                )}
                {digest!.queued_count > 0 && (
                  <span>{digest!.queued_count} queued</span>
                )}
              </div>

              {/* Direction progress */}
              {digest!.direction_progress && digest!.direction_progress.length > 0 && (
                <DirectionProgressSection directions={digest!.direction_progress} />
              )}

              {/* Needs attention */}
              {(digest!.needs_attention ?? []).length > 0 && (() => {
                const visibleItems = digest!.needs_attention.filter(item => !isHidden(item.task_id));
                const hiddenItems = digest!.needs_attention.filter(item => isHidden(item.task_id));
                const countLabel = hiddenItems.length > 0
                  ? `${visibleItems.length} visible / ${digest!.needs_attention.length} total`
                  : String(digest!.needs_attention.length);
                return (
                  <div className="border border-amber-500/30 rounded-lg p-3 flex flex-col gap-2">
                    <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-400 uppercase tracking-widest">
                      <AlertTriangle className="w-3 h-3" />
                      <span className="flex-1">Needs attention ({countLabel})</span>
                      {onBulkApprove && visibleItems.filter(i => i.reason === 'approval_required').length >= 2 && (
                        <button
                          className="text-[10px] font-semibold px-2 py-0.5 rounded border flex items-center gap-1 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20 normal-case tracking-normal"
                          onClick={() => handleBulkApprove(visibleItems.filter(i => i.reason === 'approval_required').map(i => i.task_id))}
                        >
                          <Check className="w-3 h-3" />
                          Approve all ({visibleItems.filter(i => i.reason === 'approval_required').length})
                        </button>
                      )}
                      {onBulkRetry && visibleItems.filter(i => i.reason === 'failed').length >= 2 && (
                        <button
                          className="text-[10px] font-semibold px-2 py-0.5 rounded border flex items-center gap-1 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20 normal-case tracking-normal"
                          onClick={() => handleBulkRetry(visibleItems.filter(i => i.reason === 'failed').map(i => i.task_id))}
                        >
                          <RotateCcw className="w-3 h-3" />
                          Retry all ({visibleItems.filter(i => i.reason === 'failed').length})
                        </button>
                      )}
                    </div>
                    <div className="flex flex-col gap-1.5">
                      {visibleItems.map(item => (
                        <div key={item.task_id} className="flex flex-col gap-1">
                          <div
                            role="button"
                            tabIndex={0}
                            className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                            onClick={() => onSelectTask({ id: item.task_id })}
                            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: item.task_id }); } }}
                          >
                            <span className="flex-1 truncate text-neutral-300">{item.title}</span>
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
                            <WaitingBadge hours={item.waiting_hours} />
                            {item.reason === 'approval_required' && onApprove && (
                              <button
                                className="text-[9px] font-semibold px-1.5 py-0.5 rounded border flex items-center gap-0.5 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20"
                                onClick={e => { e.stopPropagation(); handleApprove(item.task_id); }}
                              >
                                <Check className="w-2.5 h-2.5" />
                                Approve
                              </button>
                            )}
                            {item.reason === 'failed' && onRetry && (
                              <button
                                className="text-[9px] font-semibold px-1.5 py-0.5 rounded border flex items-center gap-0.5 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20"
                                onClick={e => { e.stopPropagation(); handleRetry(item.task_id); }}
                              >
                                <RotateCcw className="w-2.5 h-2.5" />
                                Retry
                              </button>
                            )}
                            {/* Dismiss/snooze button */}
                            <div className="relative shrink-0" onClick={e => e.stopPropagation()}>
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
                                  <span className="flex-1 truncate text-neutral-400">{item.title}</span>
                                  {entry?.type === 'snooze' && (
                                    <span className="shrink-0 text-[9px] font-mono text-neutral-500">
                                      {formatSnoozeRemaining((entry as { type: 'snooze'; until: number }).until)}
                                    </span>
                                  )}
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
              {digest!.active_count > 0 && (
                <div>
                  <button
                    onClick={() => setActiveExpanded(v => !v)}
                    aria-expanded={activeExpanded}
                    className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                  >
                    {activeExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    <Loader2 className="w-3 h-3 text-neutral-500" />
                    In progress ({digest!.active_count})
                  </button>
                  {activeExpanded && (
                    <div className="flex flex-col gap-1.5">
                      {(digest!.active_tasks ?? []).map(t => (
                        <div
                          key={t.task_id}
                          role="button"
                          tabIndex={0}
                          className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                          onClick={() => onSelectTask({ id: t.task_id })}
                          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: t.task_id }); } }}
                        >
                          <span className="flex-1 truncate text-neutral-400">{t.title}</span>
                          {t.direction_text && (
                            <span
                              className="shrink-0 inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 max-w-[180px] truncate"
                              title={t.direction_text}
                            >
                              <Compass className="w-2.5 h-2.5 shrink-0" />
                              <span className="truncate">{t.direction_text.slice(0, 30)}{t.direction_text.length > 30 ? '…' : ''}</span>
                            </span>
                          )}
                          {t.scope && (
                            <span
                              className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                              title={`scope: ${t.scope}`}
                            >
                              {t.scope}
                            </span>
                          )}
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
                      {digest!.active_count > (digest!.active_tasks ?? []).length && (
                        <div className="text-[10px] text-neutral-600 px-2">
                          +{digest!.active_count - (digest!.active_tasks ?? []).length} more
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Failure taxonomy root-cause pills */}
              {digest!.failure_taxonomy_distribution && digest!.failure_taxonomy_distribution.total_failures > 0 && (() => {
                const costByTaxonomy: Record<string, number> = {};
                const costByClass = digest!.cost_by_failure_class ?? {};
                for (const g of digest!.failure_groups ?? []) {
                  const groupCost = costByClass[g.failure_class] ?? 0;
                  if (!groupCost || g.tasks.length === 0) continue;
                  const perTask = groupCost / g.tasks.length;
                  for (const t of g.tasks) {
                    if (!t.taxonomy) continue;
                    costByTaxonomy[t.taxonomy] = (costByTaxonomy[t.taxonomy] ?? 0) + perTask;
                  }
                }
                const history = digest!.trend_history ?? [];
                const trendFor = (cat: string): {arrow: string; color: string} => {
                  if (history.length < 4) return {arrow: '–', color: 'text-neutral-500'};
                  const sumAt = (arr: typeof history) => arr.reduce((a, p) => a + (p.failed_by_taxonomy?.[cat] ?? 0), 0);
                  const recent = sumAt(history.slice(-2));
                  const prior = sumAt(history.slice(-4, -2));
                  if (recent > prior) return {arrow: '↑', color: 'text-red-400'};
                  if (recent < prior) return {arrow: '↓', color: 'text-emerald-400'};
                  return {arrow: '–', color: 'text-neutral-500'};
                };
                return (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Root cause</span>
                    {TAXONOMY_ORDER.map(cat => {
                      const entry = digest!.failure_taxonomy_distribution!.categories[cat];
                      if (!entry || entry.count === 0) return null;
                      const cost = costByTaxonomy[cat];
                      const trend = trendFor(cat);
                      const isActive = activeTaxonomyFilter === cat;
                      return (
                        <button
                          key={cat}
                          type="button"
                          onClick={() => {
                            setActiveTaxonomyFilter(isActive ? null : cat);
                            setActiveStageFilter(null);
                            setFailuresExpanded(true);
                          }}
                          aria-pressed={isActive}
                          className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded border transition-all ${TAXONOMY_COLORS[cat]} ${isActive ? 'ring-2 ring-white/60' : 'hover:opacity-80'}`}
                          title={`${TAXONOMY_LABELS[cat]}: ${entry.count} (${Math.round(entry.percentage)}%)${cost ? ` · $${cost.toFixed(2)}` : ''} — click to ${isActive ? 'clear' : 'filter'}`}
                        >
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
              {digest!.failure_stage_distribution && digest!.failure_stage_distribution.total_classified > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Pipeline stage</span>
                  {STAGE_ORDER.map(stage => {
                    const count = digest!.failure_stage_distribution!.stages[stage];
                    if (!count) return null;
                    const isActive = activeStageFilter === stage;
                    return (
                      <button
                        key={stage}
                        type="button"
                        onClick={() => {
                          setActiveStageFilter(isActive ? null : stage);
                          setActiveTaxonomyFilter(null);
                          setFailuresExpanded(true);
                        }}
                        aria-pressed={isActive}
                        className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded border transition-all ${STAGE_COLORS[stage] ?? 'bg-neutral-700/20 text-neutral-400 border-neutral-700/30'} ${isActive ? 'ring-2 ring-white/60' : 'hover:opacity-80'}`}
                        title={`${stage}: ${count} — click to ${isActive ? 'clear' : 'filter'}`}
                      >
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
              )}

              {/* Scope conversion rate pills */}
              {digest!.scope_conversion_rates && Object.keys(digest!.scope_conversion_rates).length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope conversion</span>
                  {['small', 'medium', 'large']
                    .filter(s => digest!.scope_conversion_rates?.[s] != null)
                    .map(scope => {
                      const { created, merged, calibrated_in = 0, calibrated_out = 0 } = digest!.scope_conversion_rates![scope];
                      const recalCount = (calibrated_in || 0) + (calibrated_out || 0);
                      const rate = created > 0 ? merged / created : null;
                      const prev = digest!.prev_scope_conversion_rates?.[scope];
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
              )}

              {/* Scope mismatch diagnostics */}
              {digest!.scope_mismatch_summary && digest!.scope_mismatch_summary.total_calibrated > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope mismatch</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest!.scope_mismatch_summary.total_calibrated} calibrated
                    </span>
                    {digest!.scope_mismatch_summary.calibrated_success_rate != null && (
                      <span className="text-[10px] text-neutral-500">
                        · {Math.round(digest!.scope_mismatch_summary.calibrated_success_rate * 100)}% success
                      </span>
                    )}
                    {Object.entries(digest!.scope_mismatch_summary.by_original_scope).map(([orig, b]) => (
                      <span key={orig} className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 px-1.5 py-0.5 rounded">
                        {orig}: {b.total}
                      </span>
                    ))}
                  </div>
                  {digest!.scope_mismatch_summary.top_directions.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {digest!.scope_mismatch_summary.top_directions.map(d => (
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
              {digest!.rejection_pattern_summary && digest!.rejection_pattern_summary.total_rejected > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Rejection patterns</span>
                    <button
                      type="button"
                      onClick={() => setFailuresExpanded(true)}
                      className="text-[10px] text-neutral-400 hover:text-purple-300 underline decoration-dotted underline-offset-2"
                      title="Expand recent failures to find rejected tasks"
                    >
                      {digest!.rejection_pattern_summary.total_rejected} rejected
                    </button>
                  </div>
                  {digest!.rejection_pattern_summary.top_notes.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {digest!.rejection_pattern_summary.top_notes.map((n, i) => (
                        <span key={i} className="text-[10px] text-neutral-500 truncate">
                          · {n.snippet.slice(0, 100)}{n.snippet.length > 100 ? '…' : ''}{' '}
                          <span className="text-purple-400">×{n.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Systemic errors — clusters of failures with matching error fingerprints */}
              {(digest!.recurring_error_clusters ?? []).length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Systemic errors</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest!.recurring_error_clusters!.length} cluster{digest!.recurring_error_clusters!.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1 ml-1">
                    {(() => {
                      const dirTextById = new Map<string, string>(
                        (digest!.direction_progress ?? []).map(dp => [dp.direction_id, dp.direction_text || dp.direction_id]),
                      );
                      return digest!.recurring_error_clusters!.map(cluster => {
                        const snippet = cluster.error_snippet.length > 120
                          ? cluster.error_snippet.slice(0, 120) + '…'
                          : cluster.error_snippet;
                        const dirCount = cluster.direction_ids.length;
                        const dirLabels = cluster.direction_ids
                          .map(id => dirTextById.get(id))
                          .filter((s): s is string => !!s)
                          .map(s => s.length > 30 ? s.slice(0, 30) + '…' : s);
                        return (
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
                              {dirLabels.slice(0, 2).map((label, i) => (
                                <span key={i} className="text-[9px] text-neutral-500 truncate max-w-[180px]">
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
              )}

              {/* Review cap exhaustion summary */}
              {digest!.review_cap_exhaustion_summary && digest!.review_cap_exhaustion_summary.count > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Review cap exhaustion</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest!.review_cap_exhaustion_summary.count} task{digest!.review_cap_exhaustion_summary.count !== 1 ? 's' : ''} hit cap
                    </span>
                    {digest!.review_cap_exhaustion_summary.avg_iterations != null && (
                      <span className="text-[10px] text-neutral-500">
                        · avg {digest!.review_cap_exhaustion_summary.avg_iterations.toFixed(1)} iter
                      </span>
                    )}
                  </div>
                  {digest!.review_cap_exhaustion_summary.top_directions.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {digest!.review_cap_exhaustion_summary.top_directions.map(d => (
                        <span key={d.direction_id} className="text-[10px] text-neutral-500 truncate">
                          · {d.direction_id}{' '}
                          <span className="text-red-400">×{d.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Retry pattern summary */}
              {digest!.retry_pattern_summary && digest!.retry_pattern_summary.total_multi_attempt_failures > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry patterns</span>
                    <span className="text-[10px] text-neutral-400">
                      {digest!.retry_pattern_summary.total_multi_attempt_failures} multi-attempt
                    </span>
                    {digest!.retry_pattern_summary.total_multi_attempt_failures > 0 && (
                      <span className="text-[10px] text-neutral-500">
                        · {Math.round((digest!.retry_pattern_summary.repeat_class_count / digest!.retry_pattern_summary.total_multi_attempt_failures) * 100)}% same class
                      </span>
                    )}
                  </div>
                  {digest!.retry_pattern_summary.top_patterns.length > 0 && (
                    <div className="flex flex-col gap-0.5 ml-1">
                      {digest!.retry_pattern_summary.top_patterns.slice(0, 3).map((p, i) => (
                        <span key={i} className="text-[10px] text-neutral-500 truncate font-mono">
                          · {p.sequence}{' '}
                          <span className="text-amber-400">×{p.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}


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
                              className="flex items-center gap-2 w-full text-left hover:opacity-80 transition-opacity mb-1"
                            >
                              {isGroupExpanded ? <ChevronUp className="w-3 h-3 text-neutral-500 shrink-0" /> : <ChevronDown className="w-3 h-3 text-neutral-500 shrink-0" />}
                              <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${badgeClass}`}>
                                {group.label}
                              </span>
                              <span className="text-[11px] text-neutral-500">× {group.tasks.length}</span>
                              {digest!.cost_by_failure_class?.[group.failure_class] != null && digest!.cost_by_failure_class[group.failure_class] > 0 && (
                                <span className="text-[10px] text-red-400/70 font-mono" title={`Cost of ${group.label} failures`}>
                                  ${digest!.cost_by_failure_class[group.failure_class].toFixed(2)}
                                </span>
                              )}
                            </button>
                            {isGroupExpanded && (
                              <div className="flex flex-col gap-1 ml-5">
                                {group.tasks.map(t => (
                                  <div key={t.task_id} className="flex flex-col gap-0.5">
                                    <div
                                      role="button"
                                      tabIndex={0}
                                      className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
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
                                          className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[12rem]"
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

              {/* Recent completions */}
              {(digest!.recent_completions ?? []).length > 0 && (() => {
                const completions = digest!.recent_completions;
                // Group by task_type, preserving insertion order
                const groups = new Map<string, RecentCompletion[]>();
                for (const c of completions) {
                  const key = c.task_type || 'other';
                  if (!groups.has(key)) groups.set(key, []);
                  groups.get(key)!.push(c);
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
