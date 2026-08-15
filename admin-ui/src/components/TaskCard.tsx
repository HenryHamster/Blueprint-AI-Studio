/* eslint-disable react-hooks/set-state-in-effect, react-refresh/only-export-components */
import React, { useState, useEffect, useRef, memo, forwardRef } from 'react';
import { CheckCircle, AlertTriangle, Clock, Hash, Activity, ExternalLink, Terminal, RotateCcw, Pencil, X, XCircle, RefreshCw, ChevronRight, ChevronDown, ChevronUp, Search, GitFork, Microscope, Shield, Archive, BookOpen, ShieldAlert, TestTube, ShieldCheck, Package, ArrowUpDown, Wrench, MoreHorizontal, Radio, Layers, Gauge, Compass, FileText, Copy, Check } from 'lucide-react';
import { formatDurationMs, parseTimestamp } from '../utils/time';
import { formatVerdict, verdictToneBadgeClass } from '../utils/verdictFormatting';
import { taskStatusBadgeClasses } from '../utils/taskStatus';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';
import { authGet, API_BASE_URL } from '../api';
import { EXIT_REASON_COLORS } from '../utils/fixLoopColors';
import { useTaskCapabilities } from '../taskCapabilities';
import { buildCancelTitle, isCancellableTaskStatus } from './taskCancelCopy';
import type { FixLoopSummary } from '../types/task';
import { deriveSpawnGateState } from '../types/task';

/** Format seconds as compact "30s", "5m", "1h" for HB age / queue wait labels. */
function formatCompact(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

export type { ResearchFinding } from '../types/task';
import type { ResearchFinding } from '../types/task';

// SR badge cluster (verdict, disallowed-blind-spot, divergence) extracted to
// ./SrBadges. Re-exported below under the same names to preserve the public API.
import { SrBadge, SrDisallowedBadge, SrDivergenceBadge } from './SrBadges';
export { SrBadge, SrDisallowedBadge, SrDivergenceBadge } from './SrBadges';

function SpawnSignalBadges({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const lowConfidence = ctx?.low_confidence_spawn === true;
  const autoDecomposed = ctx?.auto_decomposed === true;
  const mediumExploration = ctx?.medium_exploration === true;
  const spawnGate = deriveSpawnGateState(task.context);

  if (!lowConfidence && !autoDecomposed && !mediumExploration && spawnGate.kind !== 'fail-open-pass' && spawnGate.kind !== 'challenged-pass') return null;

  const rate = ctx?.low_confidence_spawn_rate;
  const ratePct = typeof rate === 'number' ? Math.round(rate * 100) : null;
  const lowConfLabel = ratePct !== null ? `${ratePct}%` : 'low-conf';
  const lowConfTooltip = ratePct !== null
    ? `Spawned at ${ratePct}% predicted merge probability`
    : 'Spawned below the configured merge-probability threshold';

  return (
    <>
      {lowConfidence && (
        <span
          title={lowConfTooltip}
          className="inline-flex items-center text-ui-sm font-medium border px-1.5 py-0.5 rounded bg-orange-500/10 border-orange-500/20 text-orange-400"
        >
          {lowConfLabel}
        </span>
      )}
      {autoDecomposed && (
        <span
          title="Finding was auto-decomposed by co-change clustering"
          className="inline-flex items-center text-ui-sm font-medium border px-1.5 py-0.5 rounded bg-neutral-700/30 border-neutral-600/30 text-neutral-400"
        >
          auto-split
        </span>
      )}
      {mediumExploration && (
        <span
          title="Medium-scope task kept despite low confidence as an exploration bypass"
          className="inline-flex items-center text-ui-sm font-medium border px-1.5 py-0.5 rounded bg-neutral-700/30 border-neutral-600/30 text-neutral-400"
        >
          explore
        </span>
      )}
      {spawnGate.kind === 'fail-open-pass' && (
        <span
          title={spawnGate.reason ?? 'Passed by fail-open — no challenger verdict was returned for this finding'}
          className="inline-flex items-center text-ui-sm font-medium border px-1.5 py-0.5 rounded bg-amber-500/10 border-amber-500/20 text-amber-400"
        >
          fail-open
        </span>
      )}
      {spawnGate.kind === 'challenged-pass' && (
        <span
          title={spawnGate.reason ?? 'Challenger confirmed this finding as actionable'}
          className="inline-flex items-center text-ui-sm font-medium border px-1.5 py-0.5 rounded bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
        >
          gate ✓
        </span>
      )}
    </>
  );
}

function FixLoopBadge({ summary }: { summary: FixLoopSummary[] }) {
  // Show up to 2 badges: non_success first, then high-iteration-count
  const sorted = [...summary].sort((a, b) => {
    if (a.non_success !== b.non_success) return a.non_success ? -1 : 1;
    return b.iterations - a.iterations;
  }).slice(0, 2);

  return (
    <>
      {sorted.map((entry) => {
        const colorClass = entry.last_exit_reason
          ? (EXIT_REASON_COLORS[entry.last_exit_reason] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30')
          : 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
        const label = entry.last_exit_reason?.startsWith('divergence_')
          ? `${entry.loop} div`
          : `${entry.loop} ×${entry.iterations}`;
        const tooltip = `${entry.loop}: ${entry.iterations} iteration${entry.iterations !== 1 ? 's' : ''}, ${entry.last_exit_reason ?? 'unknown'}`;
        return (
          <span
            key={entry.loop}
            title={tooltip}
            className={`inline-flex items-center gap-0.5 text-ui-sm font-medium border px-1.5 py-0.5 rounded ${colorClass}`}
          >
            <AlertTriangle className="w-3 h-3 shrink-0" />
            {label}
          </span>
        );
      })}
    </>
  );
}

export function isSafeUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:';
  } catch {
    return false;
  }
}

export function isResolvableStorageUri(url: string): boolean {
  return /^gs:\/\/\S+$/i.test(url);
}

async function openResolvedArtifactReference(
  projectId: string,
  reference: string,
): Promise<boolean> {
  const pendingTab = window.open('', '_blank');
  try {
    const { resolveArtifactDownloadUrl } = await import('../api');
    const resolvedUrl = await resolveArtifactDownloadUrl(projectId, reference);
    if (!isSafeUrl(resolvedUrl)) {
      throw new Error('resolver returned an unsafe URL');
    }
    if (pendingTab) {
      pendingTab.opener = null;
      pendingTab.location.replace(resolvedUrl);
    } else {
      window.open(resolvedUrl, '_blank', 'noopener,noreferrer');
    }
    return true;
  } catch {
    pendingTab?.close();
    return false;
  }
}

interface ResolvedReferenceLinkProps {
  projectId?: string | null;
  reference: string;
  className: string;
  children?: React.ReactNode;
}

export function ResolvedReferenceLink({
  projectId,
  reference,
  className,
  children,
}: ResolvedReferenceLinkProps) {
  const [opening, setOpening] = useState(false);
  const label = children ?? reference;

  if (isSafeUrl(reference)) {
    return (
      <a
        href={reference}
        target="_blank"
        rel="noopener noreferrer"
        className={className}
      >
        {label}
      </a>
    );
  }

  if (!projectId || !isResolvableStorageUri(reference)) {
    return <span className={className}>{label}</span>;
  }

  return (
    <button
      type="button"
      onClick={() => {
        if (opening) return;
        setOpening(true);
        void openResolvedArtifactReference(projectId, reference).finally(() => {
          setOpening(false);
        });
      }}
      disabled={opening}
      className={className}
      title={reference}
    >
      {label}
    </button>
  );
}

export function linkifyStatusMessage(msg: string, prUrl?: string): React.ReactNode {
  // Derive base URL from pr_url, e.g. https://github.com/owner/repo/pull/42 -> https://github.com/owner/repo/pull/
  let baseUrl: string | undefined;
  if (prUrl) {
    const m = prUrl.match(/^(.+\/pull\/)\d+/);
    if (m) baseUrl = m[1];
  }
  if (!baseUrl) return msg;

  const regex = /PR #(\d+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(msg)) !== null) {
    if (lastIndex < match.index) parts.push(msg.slice(lastIndex, match.index));
    const url = `${baseUrl}${match[1]}`;
    if (isSafeUrl(url)) {
      parts.push(
        <a key={match.index} href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 underline">
          {match[0]}
        </a>
      );
    } else {
      parts.push(match[0]);
    }
    lastIndex = match.index + match[0].length;
  }

  if (parts.length === 0) return msg;
  if (lastIndex < msg.length) parts.push(msg.slice(lastIndex));
  return <>{parts}</>;
}

export type { Task, TaskStatus, TaskType } from '../types/task';
import type { Task, TaskStatus, TaskType } from '../types/task';

export const TASK_TYPE_STYLES: Record<string, { bg: string; text: string; icon: typeof Search; label: string }> = {
  create: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-400', icon: Terminal, label: 'Create' },
  plan: { bg: 'bg-purple-500/10 border-purple-500/20', text: 'text-purple-400', icon: GitFork, label: 'Plan' },
  research: { bg: 'bg-cyan-500/10 border-cyan-500/20', text: 'text-cyan-400', icon: Microscope, label: 'Research' },
  review: { bg: 'bg-sky-500/10 border-sky-500/20', text: 'text-sky-400', icon: Search, label: 'Review' },
  fix: { bg: 'bg-orange-500/10 border-orange-500/20', text: 'text-orange-400', icon: RotateCcw, label: 'Fix' },
  refactor: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: Wrench, label: 'Refactor' },
  docs: { bg: 'bg-indigo-500/10 border-indigo-500/20', text: 'text-indigo-400', icon: BookOpen, label: 'Docs' },
  security_audit: { bg: 'bg-red-500/10 border-red-500/20', text: 'text-red-400', icon: ShieldAlert, label: 'Security Audit' },
  qa: { bg: 'bg-green-500/10 border-green-500/20', text: 'text-green-400', icon: TestTube, label: 'QA' },
  deps: { bg: 'bg-blue-500/10 border-blue-500/20', text: 'text-blue-400', icon: Package, label: 'Deps' },
  migrate: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: ArrowUpDown, label: 'Migrate' },
  investigate: { bg: 'bg-yellow-500/10 border-yellow-500/20', text: 'text-yellow-400', icon: Search, label: 'Investigate' },
  debug: { bg: 'bg-rose-500/10 border-rose-500/20', text: 'text-rose-400', icon: AlertTriangle, label: 'Debug' },
  smoketest: { bg: 'bg-lime-500/10 border-lime-500/20', text: 'text-lime-400', icon: Activity, label: 'Smoketest' },
  rollup: { bg: 'bg-violet-500/10 border-violet-500/20', text: 'text-violet-400', icon: Layers, label: 'Rollup' },
  perf: { bg: 'bg-pink-500/10 border-pink-500/20', text: 'text-pink-400', icon: Gauge, label: 'Perf' },
  strategic_review: { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-400', icon: Compass, label: 'Strategic Review' },
  design_doc_update: { bg: 'bg-slate-500/10 border-slate-500/20', text: 'text-slate-400', icon: FileText, label: 'Design Doc' },
  self_audit: { bg: 'bg-fuchsia-500/10 border-fuchsia-500/20', text: 'text-fuchsia-400', icon: Shield, label: 'Self Audit' },
  memo: { bg: 'bg-stone-500/10 border-stone-500/20', text: 'text-stone-400', icon: FileText, label: 'Memo' },
  verify_tests: { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-400', icon: ShieldCheck, label: 'Verify Tests' },
};

function parsePhase(statusMessage: string): string {
  // Strip '[XXmb]' heap prefix if present
  const msg = statusMessage.replace(/^\[\d+(\.\d+)?MB\]\s*/i, '');
  if (/cloning/i.test(msg)) return 'Cloning repo';
  if (/running claude/i.test(msg)) return 'Working';
  const toolMatch = msg.match(/using tool:\s*(\S+)/i);
  if (toolMatch) return `Using ${toolMatch[1]}`;
  if (/pushing/i.test(msg)) return 'Pushing changes';
  if (/creating pull request/i.test(msg)) return 'Creating PR';
  if (/running test|running lint|running pre-commit/i.test(msg)) return 'Running checks';
  if (/rebasing/i.test(msg)) return 'Rebasing';
  if (/spawning/i.test(msg)) return 'Spawning subtasks';
  if (/addressing review/i.test(msg)) return 'Fixing review feedback';
  if (/strategic/i.test(msg)) return 'Strategic analysis';
  return msg.slice(0, 40).trim();
}

const FAILURE_CLASS_CONFIG: Record<string, { label: string; color: string; hint: string; suggestion: string; retryLabel: string }> = {
  CODE_ERROR: { label: 'Code Error', color: 'bg-red-500/20 text-red-400', hint: 'Application code error — review error logs for root cause', suggestion: 'Review error logs before retrying', retryLabel: 'Retry' },
  PROVISION_FAILURE: { label: 'Infra', color: 'bg-orange-500/20 text-orange-400', hint: 'VM startup failed — check zone health. Auto-retries on different zone', suggestion: 'VM startup failed — will use alternate zone on retry', retryLabel: 'Retry' },
  AGENT_TIMEOUT: { label: 'Timeout', color: 'bg-yellow-500/20 text-yellow-400', hint: 'Agent died or timed out — possible OOM or stuck execution', suggestion: 'Agent process died — check for OOM or stuck execution', retryLabel: 'Retry' },
  EXTERNAL_API: { label: 'API Error', color: 'bg-blue-500/20 text-blue-400', hint: 'External service failure — typically transient, auto-retries', suggestion: 'Transient service error — typically auto-retries', retryLabel: 'Retry' },
  RATE_LIMIT: { label: 'Rate Limit', color: 'bg-purple-500/20 text-purple-400', hint: 'API quota exhausted — auto-retries with different account', suggestion: 'API quota exhausted — auto-retries with different account', retryLabel: 'Retry' },
  SCOPE_OVERFLOW: { label: 'Scope Overflow', color: 'bg-teal-500/20 text-teal-400', hint: 'Task exceeded its scope budget — consider decomposition or scope escalation', suggestion: 'Decompose into smaller tasks or escalate scope before retrying', retryLabel: 'Retry' },
  MIGRATION_ERROR: { label: 'Migration Error', color: 'bg-rose-500/20 text-rose-400', hint: 'Broken Alembic chain or EXPECTED_SCHEMA_REVISION mismatch — a real schema defect', suggestion: 'Fix the migration chain / schema revision before retrying', retryLabel: 'Retry' },
  VIEWPORT_REGRESSION: { label: 'Viewport Regression', color: 'bg-fuchsia-500/20 text-fuchsia-400', hint: 'Real mobile/viewport rendering regression — overflow, truncation, or touch-target', suggestion: 'Fix the viewport regression before retrying', retryLabel: 'Retry' },
  agent_stalled: { label: 'Agent Stalled', color: 'bg-amber-500/20 text-amber-400', hint: 'Agent was alive but made no progress — wedged execution', suggestion: 'Wedged agent — retries with a fresh agent', retryLabel: 'Retry fresh' },
  BLOCKED_PRECONDITION: { label: 'Blocked — precondition', color: 'bg-sky-500/20 text-sky-400', hint: 'Agent correctly refused on an unmet precondition (e.g. upstream PR/file not yet on main) — not a code defect', suggestion: 'Resolve the upstream precondition, then retry', retryLabel: 'Retry' },
  SCOPE_EMPTY: { label: 'Scope Empty', color: 'bg-slate-500/20 text-slate-400', hint: 'Declared files were already covered by a sibling PR — agent exited cleanly, not a code defect', suggestion: 'No action needed — work was already covered elsewhere', retryLabel: 'Retry' },
  park_wake_timeout: { label: 'Park Wake Timeout', color: 'bg-amber-500/20 text-amber-400', hint: 'Parked task wake source never fired before timeout — park/wake orchestration issue', suggestion: 'Inspect the wake source and retry after park/wake delivery is healthy', retryLabel: 'Retry' },
  park_wake_stranded: { label: 'Park Wake Stranded', color: 'bg-orange-500/20 text-orange-400', hint: 'Parked task stayed stranded after its wake should have been delivered', suggestion: 'Inspect park/wake delivery health before retrying', retryLabel: 'Retry' },
};

export { FAILURE_CLASS_CONFIG };

function HighlightText({ text, query }: { text: string; query?: string }) {
  if (!query || !text) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-amber-500/30 text-amber-200 rounded-sm px-0.5">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  );
}

interface TaskCardProps {
  task: Task;
  readOnly: boolean;
  maxRetries?: number;
  actionLoading?: string;
  compact?: boolean;
  selectionMode?: boolean;
  onToggleTaskSelection?: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  colIdx?: number;
  cardIdx?: number;
  onNavigate?: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onOpenSelfReview?: () => void;
  onTrack?: (task: Task) => void;
  isTracked?: boolean;
  onDuplicate?: (task: Task) => void;
  blockingCount?: number;
  searchQuery?: string;
  onDirectionClick?: (directionId: string) => void;
}

export function useElapsedTime(startIso: string, endIso?: string): string {
  const [now, setNow] = useState(() => Date.now());
  const intervalRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const isLive = !endIso;

  useEffect(() => {
    if (!isLive) return;
    intervalRef.current = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(intervalRef.current);
  }, [isLive]);

  const start = parseTimestamp(startIso).getTime();
  const end = endIso ? parseTimestamp(endIso).getTime() : now;
  return formatDurationMs(Math.max(0, end - start));
}

type DurationStats = Record<string, { p50_seconds: number; sample_count: number }>;
const _durationStatsCache: Record<string, { data: DurationStats; fetchedAt: number }> = {};
const _durationStatsInFlight = new Map<string, Promise<DurationStats>>();
const DURATION_STATS_TTL_MS = 5 * 60 * 1000;

function fetchDurationStats(cacheKey: string, projectId?: string): Promise<DurationStats> {
  const existing = _durationStatsInFlight.get(cacheKey);
  if (existing) return existing;
  const url = projectId
    ? `${API_BASE_URL}/api/admin/task-duration-stats?project_id=${projectId}`
    : `${API_BASE_URL}/api/admin/task-duration-stats`;
  const p = authGet(url)
    .then(r => r.json())
    .then(data => {
      const d = (data?.stats ?? {}) as DurationStats;
      _durationStatsCache[cacheKey] = { data: d, fetchedAt: Date.now() };
      return d;
    })
    .finally(() => { _durationStatsInFlight.delete(cacheKey); });
  _durationStatsInFlight.set(cacheKey, p);
  return p;
}

export function useDurationStats(projectId?: string): DurationStats {
  const cacheKey = projectId ?? 'global';
  const [stats, setStats] = useState<DurationStats>(() => {
    const cached = _durationStatsCache[cacheKey];
    return (cached && Date.now() - cached.fetchedAt < DURATION_STATS_TTL_MS) ? cached.data : {};
  });
  useEffect(() => {
    const cached = _durationStatsCache[cacheKey];
    if (cached && Date.now() - cached.fetchedAt < DURATION_STATS_TTL_MS) {
      setStats(cached.data);
      return;
    }
    let cancelled = false;
    fetchDurationStats(cacheKey, projectId)
      .then(d => { if (!cancelled) setStats(d); })
      .catch(() => {});
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey]);
  return stats;
}

export function getHeartbeatStatus(lastHeartbeat?: string) {
  if (!lastHeartbeat) return { color: 'bg-neutral-600', text: 'No heartbeat' };

  const diffMinutes = Math.max(0, (new Date().getTime() - parseTimestamp(lastHeartbeat).getTime()) / 60000);
  if (diffMinutes < 2) return { color: 'bg-emerald-500', text: 'Healthy (< 2m)' };
  if (diffMinutes < 5) return { color: 'bg-amber-500', text: 'Delayed (2-5m)' };
  return { color: 'bg-red-500', text: 'Stale (> 5m)' };
}

export function TaskTypeBadge({ taskType, expanded }: { taskType?: TaskType; expanded?: boolean }) {
  // Label derives from the published capability contract (so a newly registered
  // type renders its real label, not the raw id); icon + color stay UI-local
  // since they are presentation the backend contract does not carry.
  const { getLabel } = useTaskCapabilities();
  if (!taskType) return null;
  const style = TASK_TYPE_STYLES[taskType] ?? { bg: 'bg-neutral-500/10 border-neutral-500/20', text: 'text-neutral-400', icon: Hash, label: taskType };
  const Icon = style.icon;
  const label = getLabel(taskType);
  return (
    <span
      className={`group/badge ${style.bg} ${style.text} border text-ui-sm uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-0 hover:gap-1 p-1 hover:px-2 hover:py-0.5 transition-all duration-150 ${expanded ? 'gap-1 px-2 py-0.5' : ''}`}
      title={label}
    >
      <Icon className="w-3 h-3 shrink-0" />
      <span data-viewport-allow-truncation className={`max-w-0 overflow-hidden group-hover/badge:max-w-[80px] transition-all duration-150 ${expanded ? 'max-w-[80px]' : ''}`}>
        {label}
      </span>
    </span>
  );
}

export function ResearchFindings({ findings }: { findings: ResearchFinding[] }) {
  const [expanded, setExpanded] = useState(false);

  if (!Array.isArray(findings)) return null;
  const items = expanded ? findings : findings.slice(0, 5);
  return (
    <div>
      <div className={`space-y-1 ${expanded ? 'max-h-64 overflow-y-auto custom-scrollbar' : ''}`}>
        {items.map((f, i) => (
          <div key={i} className="text-neutral-300">
            <span className={`inline-block text-ui-xs uppercase px-1 rounded mr-1 ${f.risk === 'high' ? 'bg-red-900/40 text-red-400' : f.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' : 'bg-emerald-900/40 text-emerald-400'}`}>{f.scope}/{f.risk}</span>
            {f.finding_challenge && (
              <span className={`inline-block text-ui-xs uppercase px-1 rounded mr-1 border ${
                f.finding_challenge.agreement === 'agrees' ? 'bg-emerald-900/30 text-emerald-500 border-emerald-700/30' :
                f.finding_challenge.agreement === 'disagrees' ? 'bg-red-900/30 text-red-400 border-red-700/30' :
                'bg-neutral-800 text-neutral-500 border-neutral-700/30'
              }`} title={f.finding_challenge.reason}>
                {f.finding_challenge.agreement === 'agrees' ? '✓ confirmed' :
                 f.finding_challenge.agreement === 'disagrees' ? '✗ challenged' :
                 '? indeterminate'}
              </span>
            )}
            {f.title}
            {expanded && f.description ? (
              <div className="text-ui-sm text-neutral-500 ml-1 mt-0.5">{f.description}</div>
            ) : null}
          </div>
        ))}
      </div>
      {findings.length > 5 ? (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-ui-sm text-neutral-500 hover:text-neutral-300 mt-1 transition-colors"
        >
          {expanded ? 'Show less' : `Show all ${findings.length} findings`}
        </button>
      ) : null}
    </div>
  );
}

export function StatusBadge({ status }: { status: TaskStatus }) {
  const configs: Partial<Record<TaskStatus, { icon: typeof Activity; label: string; spin?: boolean }>> = {
    PROVISIONING: { icon: Activity, label: 'Starting…', spin: true },
    EVALUATING:   { icon: Clock,    label: 'Evaluating' },
    BLOCKED:      { icon: ShieldAlert, label: 'Blocked' },
    CANCELLED:    { icon: X,        label: 'Cancelled' },
  };
  const cfg = configs[status];
  if (!cfg) return null;
  const Icon = cfg.icon;
  return (
    <span
      className={`group/status inline-flex items-center gap-0 hover:gap-1 ${taskStatusBadgeClasses(status)} text-ui-sm uppercase tracking-wider p-1 hover:px-2 hover:py-0.5 rounded font-semibold whitespace-nowrap transition-all duration-150`}
      title={cfg.label}
    >
      <Icon className={`w-3 h-3 shrink-0 ${cfg.spin ? 'animate-spin' : ''}`} />
      <span data-viewport-allow-truncation className="max-w-0 overflow-hidden group-hover/status:max-w-[120px] transition-all duration-150">
        {cfg.label}
      </span>
    </span>
  );
}

function ErrorLogsBlock({ errorLogs }: { errorLogs: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(errorLogs);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="bg-red-950/30 text-red-400 text-ui-md p-2 rounded mb-2 border border-red-900/30 font-mono max-h-20 overflow-y-auto relative group">
      {/* eslint-disable-next-line viewport/min-touch-target -- dense toolbar */}
      <button
        onClick={(e) => { e.stopPropagation(); handleCopy(); }}
        className="absolute top-2 right-2 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity bg-neutral-900/80 hover:bg-neutral-800 text-neutral-400 hover:text-emerald-400"
        title="Copy full error logs"
      >
        {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      </button>
      <span className="font-semibold block mb-1 text-red-500 font-sans">Error:</span>
      {errorLogs.slice(0, 500)}
    </div>
  );
}

function PlanPreview({ subtasks }: { subtasks: Array<{ title: string; depends_on_indices?: number[] }> }) {
  if (!subtasks.length) return null;
  return (
    <div className="bg-purple-950/20 text-purple-300 text-ui-md p-2.5 rounded-lg mb-3 border border-purple-900/30 max-h-32 overflow-y-auto">
      <span className="font-semibold block mb-1 text-purple-400 font-sans">Plan ({subtasks.length} subtasks):</span>
      {subtasks.map((s, i) => (
        <div key={i} className="mb-0.5 text-neutral-300 font-mono">
          {(s.depends_on_indices ?? []).length > 0 ? '  \u2514\u2500 ' : ''}{s.title}
        </div>
      ))}
    </div>
  );
}

export const TaskCard = memo(forwardRef<HTMLDivElement, TaskCardProps>(function TaskCard({ task, readOnly = true, actionLoading, compact, blockingCount, selectionMode, onToggleTaskSelection, onCtrlClick, colIdx, cardIdx, onNavigate, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs, onOpenSelfReview, onTrack, isTracked, onDuplicate, searchQuery, onDirectionClick }, ref) {
  const [editing, setEditing] = useState(false);
  const [editPrompt, setEditPrompt] = useState(task.description || '');
  const [descExpanded, setDescExpanded] = useState(false);
  const [statusExpanded, setStatusExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [confirmAction, setConfirmAction] = useState<'cancel' | 'dismiss' | 'archive' | null>(null);
  const [confirmCountdown, setConfirmCountdown] = useState(3);
  const [showChangesForm, setShowChangesForm] = useState(false);
  const [changesFeedback, setChangesFeedback] = useState('');
  const [showOverflowMenu, setShowOverflowMenu] = useState(false);
  const attemptNumber = task.attempt_number ?? ((task.attempt_count ?? 0) + 1);
  const overflowMenuRef = useRef<HTMLDivElement>(null);
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
    };
  }, []);

  useEffect(() => {
    if (!readOnly) return;
    if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
    if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
    confirmTimeoutRef.current = null;
    confirmIntervalRef.current = null;
    setConfirmAction(null);
    setConfirmCountdown(3);
    setEditing(false);
    setShowChangesForm(false);
    setShowOverflowMenu(false);
  }, [readOnly]);

  useEffect(() => {
    if (!showOverflowMenu) return;
    function handleClick(e: MouseEvent) {
      if (overflowMenuRef.current && !overflowMenuRef.current.contains(e.target as Node)) {
        setShowOverflowMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showOverflowMenu]);

  const handleDestructiveClick = (action: 'cancel' | 'dismiss' | 'archive', callback: () => void) => {
    if (readOnly) return;
    if (confirmAction === action) {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
      setConfirmAction(null);
      setConfirmCountdown(3);
      callback();
    } else {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
      setConfirmAction(action);
      setConfirmCountdown(3);
      let remaining = 3;
      confirmIntervalRef.current = setInterval(() => {
        remaining -= 1;
        setConfirmCountdown(remaining);
        if (remaining <= 0 && confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
      }, 1000);
      confirmTimeoutRef.current = setTimeout(() => {
        setConfirmAction(null);
        setConfirmCountdown(3);
      }, 3000);
    }
  };
  const [now30, setNow30] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow30(Date.now()), 30000);
    return () => clearInterval(id);
  }, []);

  const isChild = !!task.parent_task_id;
  // Dependency counts come from the visible/loaded task set, not a global
  // impact endpoint, so labels must keep that scope explicit.
  const cancelTitle = confirmAction === 'cancel'
    ? `Confirm cancel (${confirmCountdown}s)`
    : buildCancelTitle(task, blockingCount ?? 0);
  const dismissTitle = 'Dismiss — marks this failed task cancelled; it stays in history and will not retry';
  const archiveTitle = 'Archive — moves this task off the board to the archive (recoverable via Unarchive)';
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isExecuting = task.status === 'IN_PROGRESS';

  const hbAgeSec = task.status === 'IN_PROGRESS' && task.last_heartbeat
    ? Math.max(0, (now30 - parseTimestamp(task.last_heartbeat).getTime()) / 1000)
    : null;
  const hbAgeText = hbAgeSec !== null ? `HB ${formatCompact(hbAgeSec)}` : null;
  const hbAgeColor = hbAgeSec === null ? '' : hbAgeSec < 60 ? 'text-emerald-400' : hbAgeSec < 300 ? 'text-amber-400' : 'text-red-400';

  const queueWaitSec = task.status === 'QUEUED'
    ? (now30 - parseTimestamp(task.created_at).getTime()) / 1000
    : null;
  const queueWaitText = queueWaitSec !== null ? `Queued ${formatCompact(queueWaitSec)}` : null;
  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING' || task.status === 'EVALUATING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);
  const durationText = task.status === 'PROVISIONING'
    ? `Starting ${duration}`
    : task.status === 'EVALUATING'
      ? `Evaluating ${duration}`
      : duration;

  const durationStats = useDurationStats(task.project_id);
  const taskType = task.task_type ?? ((task.payloads?.[0] as Record<string, unknown>)?.type as string) ?? 'create';
  const movementExplanation = task.movement_explanation?.trim();
  const blockedDisplayReason = movementExplanation || task.blocked_reason;
  const parkDisplayReason = movementExplanation || task.park_reason;
  const etaLabel = (() => {
    if (task.status !== 'IN_PROGRESS') return null;
    if (!task.started_at) return null;
    const typeStat = durationStats[taskType];
    if (!typeStat || typeStat.sample_count < 3) return null;
    const elapsedSec = (now30 - parseTimestamp(task.started_at).getTime()) / 1000;
    const remaining = typeStat.p50_seconds - elapsedSec;
    if (remaining > 0) return null;
    return 'slow';
  })();

  const handleRootKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    switch (e.key) {
      case 'Enter':
        e.preventDefault();
        onViewLogs(task);
        break;
      case ' ':
        e.preventDefault();
        if (!readOnly && selectionMode && onToggleTaskSelection) {
          onToggleTaskSelection(task.id);
        } else {
          onViewLogs(task);
        }
        break;
      case 'Escape':
        e.preventDefault();
        (e.currentTarget as HTMLElement).blur();
        break;
      case 'ArrowDown':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'down');
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'up');
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'right');
        break;
      case 'ArrowLeft':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'left');
        break;
    }
  };

  const taskTypeBorderColor: Record<string, string> = {
    create: 'border-l-amber-500',
    plan: 'border-l-purple-400',
    research: 'border-l-cyan-400',
    review: 'border-l-sky-400',
    fix: 'border-l-orange-400',
    refactor: 'border-l-teal-400',
    docs: 'border-l-indigo-400',
    security_audit: 'border-l-red-400',
    qa: 'border-l-green-400',
    deps: 'border-l-blue-400',
    migrate: 'border-l-teal-400',
    investigate: 'border-l-yellow-400',
    debug: 'border-l-rose-400',
    smoketest: 'border-l-lime-400',
    rollup: 'border-l-violet-400',
    perf: 'border-l-pink-400',
    strategic_review: 'border-l-emerald-400',
    design_doc_update: 'border-l-sky-400',
    self_audit: 'border-l-fuchsia-400',
    memo: 'border-l-stone-400',
    verify_tests: 'border-l-emerald-500',
  };
  const borderColor = task.task_type ? (taskTypeBorderColor[task.task_type] ?? 'border-l-neutral-700') : 'border-l-neutral-700';

  const directionId = (task.context as Record<string, unknown> | undefined)?.direction_id as string | undefined;
  const directionTitle = (task.context as Record<string, unknown> | undefined)?.direction_title as string | undefined;

  // Truncate direction_title to ~24 chars with ellipsis, fallback to direction_id
  const directionLabel = directionTitle
    ? (directionTitle.length > 24 ? `${directionTitle.slice(0, 24)}…` : directionTitle)
    : ((directionId?.length ?? 0) > 10 ? `${directionId?.slice(0, 10)}…` : directionId);

  // Tooltip: show full title and id when title exists, otherwise just id
  const directionTooltip = directionTitle && directionId
    ? `Direction: ${directionTitle}\n\nID: ${directionId}`
    : `Direction: ${directionId}`;

  return (
    <div
      ref={ref}
      tabIndex={0}
      role="article"
      onKeyDown={handleRootKeyDown}
      onClick={(e) => {
        if (!readOnly && (e.ctrlKey || e.metaKey) && onCtrlClick) {
          e.preventDefault();
          e.stopPropagation();
          onCtrlClick(task.id);
        }
      }}
      className={`relative group bg-neutral-900 rounded-lg border border-l-4 ${borderColor} transition-shadow outline-none focus-visible:ring-2 focus-visible:ring-amber-400/50 ${compact ? 'p-2' : 'px-3 py-2.5'} ${task.status === 'CANCELLED' ? 'opacity-60' : ''} ${isChild ? 'border-y-neutral-800 border-r-neutral-800 shadow-sm' : 'border-y-neutral-800 border-r-neutral-800 shadow-sm hover:shadow-md hover:border-y-neutral-700 hover:border-r-neutral-700'}`}
    >
      <div className="flex items-start gap-2 mb-1.5">
        <div className="flex-1 min-w-0">
          <h3 className={`font-medium text-sm ${task.status === 'CANCELLED' ? 'text-neutral-500 line-through' : 'text-neutral-100'}`}><HighlightText text={task.title} query={searchQuery} /></h3>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {/* Hover quick-action toolbar — always visible on mobile (≤ sm), hover-only on desktop */}
          {!readOnly && <div className="flex items-center gap-0.5 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity duration-150">
            {task.requires_approval && (task.status === 'QUEUED' || task.status === 'EVALUATING') && (
              <button
                title="Approve"
                onClick={(e) => { e.stopPropagation(); onApprove(task.id); }}
                disabled={!!actionLoading}
                className="sm:w-6 sm:h-6 sm:p-0 p-1.5 flex items-center justify-center rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto"
              >
                <CheckCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {isCancellableTaskStatus(task.status) && (
              <button
                title={cancelTitle}
                onClick={(e) => { e.stopPropagation(); handleDestructiveClick('cancel', () => onCancel(task.id)); }}
                disabled={!!actionLoading}
                className={`sm:w-6 sm:h-6 sm:p-0 p-1.5 flex items-center justify-center rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto ${confirmAction === 'cancel' ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100'}`}
              >
                <XCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {task.status === 'FAILED' && (
              <button
                title="Retry"
                onClick={(e) => { e.stopPropagation(); onRetry(task.id); }}
                disabled={!!actionLoading}
                className="sm:w-6 sm:h-6 sm:p-0 p-1.5 flex items-center justify-center rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            )}
            {(task.status === 'COMPLETED' || task.status === 'CANCELLED' || task.status === 'FAILED') && (
              <button
                title={confirmAction === 'archive' ? `Confirm archive (${confirmCountdown}s)` : archiveTitle}
                onClick={(e) => { e.stopPropagation(); handleDestructiveClick('archive', () => onArchive(task.id)); }}
                disabled={!!actionLoading}
                className={`sm:w-6 sm:h-6 sm:p-0 p-1.5 flex items-center justify-center rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100'}`}
              >
                <Archive className="w-3.5 h-3.5" />
              </button>
            )}
            {onDuplicate && (
              <button
                title="Duplicate task"
                onClick={(e) => { e.stopPropagation(); onDuplicate(task); }}
                className="sm:w-6 sm:h-6 sm:p-0 p-1.5 flex items-center justify-center rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100 transition-colors min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto"
              >
                <Copy className="w-3.5 h-3.5" />
              </button>
            )}
          </div>}
          <StatusBadge status={task.status} />
          {isExecuting && task.last_heartbeat && (
            <div className="flex items-center gap-0.5" title={heartbeat.text}>
              <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${heartbeat.color}`} />
              {hbAgeText && <span className={`text-ui-sm font-mono ${hbAgeColor}`}>{hbAgeText}</span>}
            </div>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1.5 flex-wrap mb-1.5">
        <TaskTypeBadge taskType={task.task_type} />
        {task.requires_approval && (
          <span className="bg-amber-500/10 text-amber-500 border border-amber-500/20 text-ui-sm uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-1">
            <Shield className="w-3 h-3" />Approval
          </span>
        )}
        <span className={`text-ui-sm px-1.5 py-0.5 rounded font-mono ${attemptNumber > 1 ? 'bg-neutral-800 text-neutral-300' : 'bg-neutral-800/50 text-neutral-500'}`}>
          #{attemptNumber}
        </span>
        {task.fix_loop_summary && task.fix_loop_summary.length > 0 && (
          <FixLoopBadge summary={task.fix_loop_summary} />
        )}
        {(blockingCount ?? 0) > 0 && (task.status === 'FAILED' || task.status === 'CANCELLED') && (
          <span className="inline-flex items-center gap-1 text-ui-sm font-medium bg-red-500/10 border border-red-500/30 text-red-400 px-1.5 py-0.5 rounded">
            <AlertTriangle className="w-3 h-3" />
            Blocking {blockingCount} visible task{(blockingCount ?? 0) > 1 ? 's' : ''}
          </span>
        )}
        {directionId && (
          onDirectionClick ? (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onDirectionClick(directionId); }}
              className="inline-flex items-center gap-0.5 text-ui-sm text-neutral-500 hover:text-neutral-200 hover:bg-neutral-800 rounded px-1 -mx-1 transition-colors cursor-pointer min-h-[44px] sm:min-h-auto"
              title={directionTooltip}
            >
              <Compass className="w-3 h-3" />
              {directionLabel}
            </button>
          ) : (
            <span
              className="inline-flex items-center gap-0.5 text-ui-sm text-neutral-500"
              title={directionTooltip}
            >
              <Compass className="w-3 h-3" />
              {directionLabel}
            </span>
          )
        )}
        <SpawnSignalBadges task={task} />
        {(isTerminal || task.status === 'EVALUATING') && (
          <SrBadge task={task} onClick={onOpenSelfReview} />
        )}
        {(isTerminal || task.status === 'EVALUATING') && (
          <SrDisallowedBadge task={task} />
        )}
        {(isTerminal || task.status === 'EVALUATING') && (
          <SrDivergenceBadge task={task} onClick={onOpenSelfReview} />
        )}
        {(isTerminal || task.status === 'EVALUATING') && task.evaluation_summary?.verdict ? (() => {
          const { label, tone } = formatVerdict(task.evaluation_summary.verdict);
          if (!label) return null;
          return (
            <span
              className={`inline-flex items-center text-ui-sm font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border ${verdictToneBadgeClass(tone)}`}
              title={`Evaluation verdict: ${label}`}
            >
              {label}
            </span>
          );
        })() : null}
        {task.status === 'COMPLETED' && task.pr_url ? (() => {
          if (task.unverified_merge_reason) {
            const label = humanizeUnverifiedReason(task.unverified_merge_reason);
            return (
              <span
                className="inline-flex items-center text-ui-sm font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-amber-500/10 border-amber-500/20 text-amber-400"
                title={`Merge unverified: ${label}`}
              >
                <AlertTriangle className="w-3 h-3 mr-1 shrink-0" />
                Unverified
              </span>
            );
          }
          const ctx = task.context as Record<string, unknown> | undefined;
          const landedOnIntegration = ctx?.landed_on_integration;
          const nonMergeReason = ctx?._non_merge_unblock_reason as string | undefined;
          if (landedOnIntegration === false || nonMergeReason) {
            return (
              <span
                className="inline-flex items-center text-ui-sm font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-neutral-500/10 border-neutral-500/20 text-neutral-400"
                title={nonMergeReason ? `Completed without merge: ${nonMergeReason}` : 'PR closed without merge'}
              >
                <X className="w-3 h-3 mr-1 shrink-0" />
                Closed
              </span>
            );
          }
          return (
            <span
              className="inline-flex items-center text-ui-sm font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
              title="Merge verified by tests"
            >
              <ShieldCheck className="w-3 h-3 mr-1 shrink-0" />
              Verified
            </span>
          );
        })() : null}
        {task.status === 'COMPLETED' && task.post_commit_pending && (
          <span
            className="inline-flex items-center text-ui-sm font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border bg-amber-500/10 border-amber-500/20 text-amber-400"
            title="Post-commit side effects (PR submission, child spawns, merge) are queued and have not fired yet"
          >
            <Clock className="w-3 h-3 mr-1 shrink-0" />
            Effects pending
          </span>
        )}
      </div>

      {isExecuting && heartbeat.text.startsWith('Stale') && (
        <div className="bg-amber-950/40 border border-amber-900/50 text-amber-400 rounded px-2 py-1.5 text-xs mb-2">
          <div className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" style={{ width: 14, height: 14 }} />
            <span className="flex-1 text-xs" data-testid="stuck-silence-duration">
              Silent {hbAgeSec !== null ? formatCompact(hbAgeSec) : '5m+'} — task may be stuck
            </span>
            <button
              onClick={(e) => { e.stopPropagation(); onViewLogs(task); }}
              className="shrink-0 text-ui-md text-amber-300 hover:text-amber-100 underline transition-colors"
              title="View logs"
            >
              Logs
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- dense inline toolbar */}
            <button
              onClick={(e) => { e.stopPropagation(); onRetry(task.id); }}
              disabled={!!actionLoading}
              className="shrink-0 text-ui-md px-1.5 py-0.5 rounded transition-colors disabled:opacity-50 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300"
              data-testid="stuck-requeue-btn"
              title="Terminate the stuck VM and re-queue this task for a fresh attempt"
            >
              Re-queue
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- dense inline toolbar */}
            <button
              onClick={(e) => { e.stopPropagation(); handleDestructiveClick('cancel', () => onCancel(task.id)); }}
              disabled={!!actionLoading}
              className={`shrink-0 text-ui-md px-1.5 py-0.5 rounded transition-colors disabled:opacity-50 ${confirmAction === 'cancel' ? 'bg-amber-500/40 text-amber-200' : 'bg-amber-500/20 hover:bg-amber-500/30 text-amber-300'}`}
            >
              {confirmAction === 'cancel' ? `Cancel (${confirmCountdown}s)` : 'Cancel'}
            </button>
          </div>
          {task.latest_status_message && (
            <p className="text-ui-sm text-amber-300/70 mt-1 pl-5 truncate" data-testid="stuck-last-activity">
              Last: {parsePhase(task.latest_status_message)}
            </p>
          )}
        </div>
      )}

      {task.status === 'IN_PROGRESS' && task.latest_status_message && (
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="relative flex h-1.5 w-1.5 shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-neutral-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-neutral-500" />
          </span>
          <span className="text-ui-md text-neutral-400 truncate">{parsePhase(task.latest_status_message)}</span>
        </div>
      )}

      {!compact && (
        <div className="mb-2">
          <p className={`text-xs text-neutral-400 ${descExpanded ? '' : 'line-clamp-1'}`}><HighlightText text={task.description} query={searchQuery} /></p>
          {task.description.length > 80 && (
            <button
              onClick={(e) => { e.stopPropagation(); setDescExpanded(!descExpanded); }}
              className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer flex items-center gap-0.5 mt-0.5 min-h-[44px] sm:min-h-auto"
            >
              {descExpanded ? <>Show less <ChevronUp className="w-3 h-3" /></> : <>Show more <ChevronDown className="w-3 h-3" /></>}
            </button>
          )}
        </div>
      )}

      {!compact && task.latest_status_message ? (
        <div className="bg-neutral-950 text-neutral-400 text-ui-md rounded-lg mb-2 border border-neutral-800/50 font-mono">
          <button
            data-viewport-allow-small-target
            onClick={() => setStatusExpanded(!statusExpanded)}
            className="w-full flex items-center gap-1.5 p-2.5 text-left hover:text-neutral-300 transition-colors"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 text-neutral-500 transition-transform ${statusExpanded ? 'rotate-90' : ''}`} />
            <span className="font-semibold text-neutral-500 font-sans text-ui-md">Agent Status:</span>
            {!statusExpanded && (
              <span className="truncate ml-1">&gt; {task.latest_status_message.split('\n')[0].slice(0, 80)}</span>
            )}
          </button>
          {statusExpanded && (
            <div className="px-2.5 pb-2.5 whitespace-pre-wrap break-words">
              &gt; {linkifyStatusMessage(task.latest_status_message, task.pr_url)}
            </div>
          )}
        </div>
      ) : null}

      {(task.status === 'COMPLETED' || task.status === 'EVALUATING') && task.pr_url && isSafeUrl(task.pr_url) ? (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-xs px-3 py-1.5 rounded mb-2 hover:bg-emerald-500/20 transition-colors font-medium"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          View Pull Request
        </a>
      ) : (task.task_type === 'review' || task.task_type === 'fix') && task.repo_url && task.payloads?.[0]?.pr_number && isSafeUrl(`${task.repo_url}/pull/${task.payloads[0].pr_number}`) ? (
        <a
          href={`${task.repo_url}/pull/${task.payloads[0].pr_number}`}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-neutral-400 hover:text-neutral-200 text-ui-md mb-2 transition-colors"
        >
          <ExternalLink className="w-3 h-3" />
          PR #{task.payloads[0].pr_number as number}
        </a>
      ) : null}

      {task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class] ? (
        <div className="mb-2">
          <button
            type="button"
            data-viewport-allow-small-target
            onClick={(e) => { e.stopPropagation(); window.dispatchEvent(new CustomEvent('blueprint:failure-class-filter', { detail: task.failure_class })); }}
            className={`inline-flex items-center text-ui-sm px-1.5 py-0.5 rounded font-semibold hover:opacity-80 transition-opacity ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}
            title={FAILURE_CLASS_CONFIG[task.failure_class].hint}
          >
            {FAILURE_CLASS_CONFIG[task.failure_class].label}
          </button>
          <p className="text-ui-sm text-neutral-500 mt-0.5">{FAILURE_CLASS_CONFIG[task.failure_class].suggestion}</p>
          {(() => {
            const retryAfter = task.context?.retry_after as string | undefined;
            if (!retryAfter) return null;
            const retryTime = parseTimestamp(retryAfter).getTime();
            const isFuture = retryTime > now30;
            if (isFuture) {
              const mins = Math.ceil((retryTime - now30) / 60000);
              return <p className="text-ui-sm text-emerald-400 mt-0.5">Auto-retrying in {mins}m</p>;
            }
            return <p className="text-ui-sm text-amber-400 mt-0.5">Auto-retry pending</p>;
          })()}
        </div>
      ) : null}

      {task.status === 'FAILED' && task.error_logs ? (
        <ErrorLogsBlock errorLogs={task.error_logs} />
      ) : null}

      {task.research_findings ? (
        <div className="bg-cyan-950/20 text-cyan-300 text-ui-md p-2 rounded mb-2 border border-cyan-900/30 font-mono">
          <span
            onClick={(e) => { e.stopPropagation(); setFindingsExpanded(!findingsExpanded); }}
            className="flex items-center gap-1 text-cyan-400 font-sans cursor-pointer mb-1"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 transition-transform ${findingsExpanded ? 'rotate-90' : ''}`} />
            {task.research_findings.length} findings
          </span>
          {findingsExpanded && (
            <div className="max-h-32 overflow-y-auto">
              <ResearchFindings findings={task.research_findings} />
            </div>
          )}
        </div>
      ) : null}

      {task.plan_subtasks && task.plan_subtasks.length > 0 ? <PlanPreview subtasks={task.plan_subtasks} /> : null}

      {(task.status === 'BLOCKED' || task.blocker_kind || task.blocked_reason) ? (
        <div className="bg-amber-950/30 border border-amber-900/40 rounded-lg px-2.5 py-2 mb-2">
          <div className="flex items-center gap-1.5 mb-0.5">
            <ShieldAlert className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span className="text-ui-md font-semibold text-amber-400">Blocked</span>
            {task.blocker_kind ? (
              <span className="text-ui-xs uppercase tracking-wider font-mono bg-amber-500/10 text-amber-300 border border-amber-500/20 px-1.5 py-0.5 rounded">
                {task.blocker_kind}
              </span>
            ) : null}
          </div>
          {blockedDisplayReason ? (
            <p className="text-ui-md text-amber-200/80 break-words">{blockedDisplayReason}</p>
          ) : (
            <p className="text-ui-md text-amber-200/60 italic">Parked on an unmet precondition</p>
          )}
          {(() => {
            if (!task.recheck_after) return null;
            const recheckTime = parseTimestamp(task.recheck_after).getTime();
            const isFuture = recheckTime > now30;
            if (isFuture) {
              const mins = Math.max(1, Math.ceil((recheckTime - now30) / 60000));
              return <p className="text-ui-sm text-amber-400/80 mt-0.5 font-mono">Rechecks in {mins}m</p>;
            }
            return <p className="text-ui-sm text-amber-400/80 mt-0.5 font-mono">Recheck due</p>;
          })()}
        </div>
      ) : null}

      {task.status === 'PARKED' ? (
        <div className="bg-violet-950/30 border border-violet-900/40 rounded-lg px-2.5 py-2 mb-2">
          <div className="flex items-center gap-1.5 mb-0.5">
            <Clock className="w-3.5 h-3.5 text-violet-400 shrink-0" />
            <span className="text-ui-md font-semibold text-violet-400">Parked</span>
            {task.wake_registration?.wake_source_kind ? (
              <span className="text-ui-xs uppercase tracking-wider font-mono bg-violet-500/10 text-violet-300 border border-violet-500/20 px-1.5 py-0.5 rounded">
                {task.wake_registration.wake_source_kind}
              </span>
            ) : null}
            {task.wake_registration?.delivery_state ? (
              <span className={`text-ui-xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded border ${
                task.wake_registration.delivery_state === 'DELIVERED' ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20' :
                task.wake_registration.delivery_state === 'FIRED' ? 'bg-blue-500/10 text-blue-300 border-blue-500/20' :
                task.wake_registration.delivery_state === 'EXPIRED' || task.wake_registration.delivery_state === 'FAILED' ? 'bg-red-500/10 text-red-300 border-red-500/20' :
                'bg-violet-500/10 text-violet-300 border-violet-500/20'
              }`}>
                {task.wake_registration.delivery_state}
              </span>
            ) : null}
          </div>
          {parkDisplayReason ? (
            <p className="text-ui-md text-violet-200/80 break-words">{parkDisplayReason}</p>
          ) : null}
          {(() => {
            if (!task.wake_registration?.deadline_at) return null;
            const deadlineTime = parseTimestamp(task.wake_registration.deadline_at).getTime();
            const isFuture = deadlineTime > now30;
            if (isFuture) {
              const mins = Math.max(1, Math.ceil((deadlineTime - now30) / 60000));
              return <p className="text-ui-sm text-violet-400/80 mt-0.5 font-mono">Wake deadline in {mins}m</p>;
            }
            return <p className="text-ui-sm text-red-400/80 mt-0.5 font-mono">Wake deadline passed</p>;
          })()}
        </div>
      ) : null}

      {task.depends_on && task.depends_on.length > 0 ? (
        <div className="text-ui-sm text-neutral-500 mb-2 font-mono flex items-center gap-1">
          <GitFork className="w-3 h-3" />
          Waiting on {task.depends_on.length} task{task.depends_on.length > 1 ? 's' : ''}
        </div>
      ) : null}

      <div className="flex items-center justify-between text-ui-sm text-neutral-500 mt-auto pt-2 border-t border-neutral-800">
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3" />
          {parseTimestamp(task.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          <span className="text-neutral-400 font-mono">
            {queueWaitText ?? durationText}
          </span>
          {etaLabel && (
            <span className={`font-mono ${etaLabel === 'slow' ? 'text-amber-400' : 'text-neutral-400'}`}>
              · {etaLabel}
            </span>
          )}
          {(task.status === 'COMPLETED' || task.status === 'FAILED') && task.estimated_cost_usd != null && (
            <span
              className={`font-mono ${task.status === 'FAILED' ? 'text-red-600' : 'text-neutral-500'}`}
              title={task.status === 'FAILED' ? 'Estimated VM cost (wasted spend)' : 'Estimated VM cost'}
            >
              · ${task.estimated_cost_usd.toFixed(2)}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {isExecuting ? (
            <div className="flex items-center gap-1.5" title={heartbeat.text}>
              <div className={`w-1.5 h-1.5 rounded-full ${heartbeat.color}`} />
              {hbAgeText && <span className={`text-ui-sm font-mono ${hbAgeColor}`}>{hbAgeText}</span>}
            </div>
          ) : null}

          {task.status !== 'QUEUED' && onTrack ? (
            <button
              onClick={(e) => { e.stopPropagation(); onTrack(task); }}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded transition-colors text-ui-sm min-h-[44px] sm:min-h-auto ${isTracked ? 'bg-blue-500/20 text-blue-400 hover:bg-blue-500/30' : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800'}`}
              title={isTracked ? 'Tracking live' : 'Track task'}
            >
              <Radio className="w-3 h-3" />
              {isTracked ? 'Live' : 'Track'}
            </button>
          ) : null}

          <button
            onClick={(e) => { e.stopPropagation(); onViewLogs(task); }}
            className="flex items-center justify-center gap-1 min-h-11 min-w-11 text-neutral-400 hover:text-neutral-200 transition-colors"
            title="View details"
          >
            <Terminal className="w-3 h-3" />
          </button>
        </div>
      </div>

      {!readOnly && task.requires_approval && !['FAILED', 'CANCELLED'].includes(task.status) ? (
        <div className={`flex flex-col gap-2 ${compact ? 'mt-2' : 'mt-4'}`}>
          <button onClick={() => onApprove(task.id)} disabled={!!actionLoading} className={`w-full bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 border border-emerald-500/30 rounded-md font-semibold transition-colors flex items-center justify-center gap-2 min-h-[44px] ${compact ? 'py-1.5 text-xs' : 'py-2.5 text-sm'} ${actionLoading && actionLoading !== 'approve' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
            {actionLoading === 'approve' ? <Activity className={compact ? 'w-3.5 h-3.5 animate-spin' : 'w-4 h-4 animate-spin'} /> : <CheckCircle className={compact ? 'w-3.5 h-3.5' : 'w-4 h-4'} />}{(task.task_type as string) === 'compute_experiment' ? (() => { const cap = (task.payloads?.[0] as Record<string, unknown> | undefined)?.spend_cap_usd; return cap != null ? `Approve Spend ($${Number(cap).toFixed(2)})` : 'Approve Spend'; })() : 'Approve & Merge'}
          </button>
          {!showChangesForm ? (
            <div className="flex gap-2">
              <button onClick={() => setShowChangesForm(true)} className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-2 min-h-[44px] rounded-md text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
                <RotateCcw className="w-3.5 h-3.5" /> Request Changes
              </button>
              <button title={confirmAction === 'cancel' ? `Confirm (${confirmCountdown}s)` : 'Reject — cancels this task without merging the PR'} onClick={() => handleDestructiveClick('cancel', () => onCancel(task.id))} disabled={!!actionLoading} className={`px-3 py-2 min-h-[44px] rounded-md text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1 border ${confirmAction === 'cancel' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800/50 hover:bg-neutral-700 text-neutral-500 border-neutral-700'}`}>
                {confirmAction === 'cancel' ? `Confirm (${confirmCountdown}s)` : 'Reject'}
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-2 bg-neutral-800/50 rounded-md p-2 border border-neutral-700">
              <textarea
                value={changesFeedback}
                onChange={e => setChangesFeedback(e.target.value)}
                placeholder="Describe what needs to change..."
                className="w-full bg-neutral-900 border border-neutral-700 rounded text-xs text-neutral-200 p-2 resize-none focus:outline-none focus:border-neutral-500 placeholder-neutral-600"
                rows={3}
              />
              <div className="flex gap-2">
                <button
                  onClick={() => { onRequestChanges(task.id, changesFeedback, 'fix'); setShowChangesForm(false); setChangesFeedback(''); }}
                  disabled={!changesFeedback.trim() || !!actionLoading}
                  className="flex-1 bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/20 py-1.5 rounded text-xs font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center gap-1.5 min-h-[44px] sm:min-h-auto"
                >
                  {actionLoading === 'request-changes' ? <Activity className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} Send Fix
                </button>
                <button
                  onClick={() => { onRequestChanges(task.id, changesFeedback || 'Re-review requested', 'review'); setShowChangesForm(false); setChangesFeedback(''); }}
                  disabled={!!actionLoading}
                  className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 min-h-[44px] sm:min-h-auto"
                >
                  <Search className="w-3 h-3" /> Re-review
                </button>
                <button onClick={() => { setShowChangesForm(false); setChangesFeedback(''); }} className="px-2 py-1.5 text-neutral-500 hover:text-neutral-300 text-xs min-h-[44px] sm:min-h-auto">
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}

      {/* Cancel/Archive for active and terminal-non-failed tasks handled via hover toolbar + drawer */}

      {!readOnly && task.status === 'FAILED' ? (
        compact ? (
          editing ? (
            <div className="mt-2 space-y-2">
              <textarea
                value={editPrompt}
                onChange={e => setEditPrompt(e.target.value)}
                className={`w-full bg-neutral-950 text-neutral-100 rounded px-2 py-1.5 text-xs resize-y focus:outline-none focus:ring-1 focus:ring-blue-500/50 min-h-[80px] font-mono border ${editPrompt.trim() === '' ? 'border-red-500/70' : 'border-neutral-700'}`}
              />
              <div className="flex gap-1.5">
                <button onClick={() => { onEditPrompt(task.id, editPrompt); setEditing(false); }}
                  disabled={!!actionLoading || editPrompt.trim() === ''}
                  className={`flex-1 border py-1 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 min-h-[44px] sm:min-h-auto ${editPrompt.trim() === '' ? 'bg-neutral-800 text-neutral-600 border-neutral-700 cursor-not-allowed' : 'bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border-blue-500/20'} ${actionLoading ? 'opacity-50 cursor-not-allowed' : ''}`}>
                  {actionLoading === 'edit' && <Activity className="w-3 h-3 animate-spin" />}Save
                </button>
                <button onClick={() => { setEditing(false); setEditPrompt(task.description || ''); }}
                  className="px-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 border border-neutral-700 py-1 rounded text-xs transition-colors min-h-[44px] sm:min-h-auto">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
          <div className="mt-2" ref={overflowMenuRef}>
            <button
              onClick={() => setShowOverflowMenu(v => !v)}
              className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 transition-colors min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto"
              title="More actions"
            >
              <MoreHorizontal className="w-3.5 h-3.5" />
            </button>
            {showOverflowMenu && (
              <div className="mt-1 flex gap-1.5">
                <button onClick={() => { if (task.failure_class === 'CODE_ERROR') { setEditing(true); setShowOverflowMenu(false); } else { onRetry(task.id); setShowOverflowMenu(false); } }} disabled={!!actionLoading} className={`flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 min-h-[44px] sm:min-h-auto ${actionLoading && actionLoading !== 'retry' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                  {actionLoading === 'retry' ? <Activity className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} {task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class] ? FAILURE_CLASS_CONFIG[task.failure_class].retryLabel : 'Retry'}
                </button>
                <button onClick={() => { setEditing(true); setShowOverflowMenu(false); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 min-h-[44px] sm:min-h-auto ${actionLoading ? 'opacity-50 pointer-events-none cursor-not-allowed' : ''}`} title="Edit prompt">
                  <Pencil className="w-3 h-3" />
                </button>
                <button onClick={() => { handleDestructiveClick('dismiss', () => { onDismiss(task.id); setShowOverflowMenu(false); }); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border min-h-[44px] sm:min-h-auto ${confirmAction === 'dismiss' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'dismiss' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title={dismissTitle}>
                  {actionLoading === 'dismiss' ? <Activity className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}{confirmAction === 'dismiss' ? ` (${confirmCountdown}s)` : ''}
                </button>
                <button onClick={() => { handleDestructiveClick('archive', () => { onArchive(task.id); setShowOverflowMenu(false); }); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border min-h-[44px] sm:min-h-auto ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'archive' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title={archiveTitle}>
                  {actionLoading === 'archive' ? <Activity className="w-3 h-3 animate-spin" /> : <Archive className="w-3 h-3" />}{confirmAction === 'archive' ? ` (${confirmCountdown}s)` : ''}
                </button>
              </div>
            )}
          </div>
          )
        ) : (
          <div className="mt-3 space-y-2">
            {editing ? (
              <div className="space-y-2">
                <textarea
                  value={editPrompt}
                  onChange={e => setEditPrompt(e.target.value)}
                  className={`w-full bg-neutral-950 text-neutral-100 rounded px-2 py-1.5 text-xs resize-y focus:outline-none focus:ring-1 focus:ring-blue-500/50 min-h-[120px] font-mono border ${editPrompt.trim() === '' ? 'border-red-500/70' : 'border-neutral-700'}`}
                />
                <div className="text-ui-sm text-neutral-500">{editPrompt.length} characters</div>
                <div className="flex gap-2">
                  <button onClick={() => { onEditPrompt(task.id, editPrompt); setEditing(false); }}
                    disabled={!!actionLoading || editPrompt.trim() === ''}
                    className={`flex-1 border py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 min-h-[44px] sm:min-h-auto ${editPrompt.trim() === '' ? 'bg-neutral-800 text-neutral-600 border-neutral-700 cursor-not-allowed' : 'bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border-blue-500/20'} ${actionLoading ? 'opacity-50 cursor-not-allowed' : ''}`}>
                    {actionLoading === 'edit' && <Activity className="w-3.5 h-3.5 animate-spin" />}Save
                  </button>
                  <button onClick={() => { setEditing(false); setEditPrompt(task.description || ''); }}
                    className="px-3 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 border border-neutral-700 py-1.5 rounded text-xs transition-colors">
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="flex gap-2">
                  <button onClick={() => onRetry(task.id)} disabled={!!actionLoading} className={`flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 min-h-[44px] sm:min-h-auto ${actionLoading && actionLoading !== 'retry' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                    {actionLoading === 'retry' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />} Retry
                  </button>
                  <button onClick={() => setEditing(true)} disabled={!!actionLoading} className={`flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 min-h-[44px] sm:min-h-auto ${actionLoading ? 'opacity-50 pointer-events-none' : ''}`}>
                    <Pencil className="w-3.5 h-3.5" /> Edit
                  </button>
                  <button onClick={() => handleDestructiveClick('dismiss', () => onDismiss(task.id))} disabled={!!actionLoading} className={`py-1.5 px-2.5 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border min-h-[44px] min-w-[44px] sm:min-h-auto sm:min-w-auto ${confirmAction === 'dismiss' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'dismiss' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title={dismissTitle}>
                    {actionLoading === 'dismiss' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <X className="w-3.5 h-3.5" />}{confirmAction === 'dismiss' ? ` (${confirmCountdown}s)` : ''}
                  </button>
                </div>
                <button onClick={() => handleDestructiveClick('archive', () => onArchive(task.id))} disabled={!!actionLoading} className={`w-full py-1.5 rounded text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1.5 border min-h-[44px] sm:min-h-auto ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'archive' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                  {actionLoading === 'archive' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <Archive className="w-3.5 h-3.5" />}
                  {confirmAction === 'archive' ? `Click again to archive (${confirmCountdown}s)` : 'Archive'}
                </button>
              </>
            )}
          </div>
        )
      ) : null}
    </div>
  );
}));
