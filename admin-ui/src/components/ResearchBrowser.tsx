import { useState, useMemo, useEffect, useCallback } from 'react';
import { X, ChevronDown, ChevronRight, Compass, Search, CheckCircle, Clock, XCircle, Minus, ExternalLink, BarChart2, GitBranch } from 'lucide-react';
import type { ResearchFinding } from './TaskCard';
import type { FindingChallenge } from '../types/task';
import { TaskTypeBadge, isSafeUrl, FAILURE_CLASS_CONFIG } from './TaskCard';
import { API_BASE_URL, authHeaders, fetchRecentFindings, type RecentFindingsResponse } from '../api';
import { parseTimestamp } from '../utils/time';
import { formatVerdict, verdictToneBadgeClass } from '../utils/verdictFormatting';
import type { Task, TaskEvaluationSummary, SpawnGateState } from '../types/task';
import { deriveSpawnGateState } from '../types/task';

interface Direction {
  id: string;
  text: string;
  status: string;
  priority?: string;
}

/** A single finding with its provenance and outcome. */
interface EnrichedFinding {
  finding: ResearchFinding;
  taskId: string;
  taskCreatedAt: string;
  findingIndex: number;
  /** Info about the child task that implemented this finding, if any. */
  outcome?: { status: string; taskType?: string; taskId: string; prUrl?: string; failureClass?: string; evaluationSummary?: TaskEvaluationSummary | null; spawnGateState?: SpawnGateState };
}

/** Findings grouped under a direction (or "undirected"). */
interface FindingGroup {
  directionId: string | null;
  directionText: string;
  directionStatus?: string;
  findings: EnrichedFinding[];
}

interface ResearchBrowserProps {
  tasks: Task[];
  selectedProjectId: string | null;
  onClose: () => void;
  onNavigateToTask?: (taskId: string) => void;
  initialDirectionId?: string;
  /** Project-wide task feed. Used for research findings and implementation child outcomes when provided. */
  researchFeed?: Task[];
}

const OUTCOME_STYLES: Record<string, { icon: typeof CheckCircle; color: string; label: string }> = {
  COMPLETED: { icon: CheckCircle, color: 'text-emerald-500', label: 'Merged' },
  IN_PROGRESS: { icon: Clock, color: 'text-cyan-400', label: 'In progress' },
  EVALUATING: { icon: Clock, color: 'text-amber-400', label: 'Evaluating' },
  QUEUED: { icon: Clock, color: 'text-neutral-400', label: 'Queued' },
  PROVISIONING: { icon: Clock, color: 'text-neutral-400', label: 'Starting' },
  BLOCKED: { icon: Clock, color: 'text-amber-400', label: 'Blocked' },
  PARKED: { icon: Clock, color: 'text-purple-400', label: 'Parked' },
  FAILED: { icon: XCircle, color: 'text-red-400', label: 'Failed' },
  CANCELLED: { icon: Minus, color: 'text-neutral-500', label: 'Cancelled' },
};

function relativeDate(dateStr: string): string {
  const diffMs = Date.now() - parseTimestamp(dateStr).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

const HOUR_MS = 60 * 60 * 1000;

function getStaleness(dateStr: string): 'fresh' | 'stale' | 'very_stale' {
  const ageMs = Date.now() - parseTimestamp(dateStr).getTime();
  if (ageMs >= 72 * HOUR_MS) return 'very_stale';
  if (ageMs >= 24 * HOUR_MS) return 'stale';
  return 'fresh';
}

interface FindingMetrics {
  mergedCount: number;
  activeCount: number;
  prCount: number;
  failedCount: number;
  proposedCount: number;
  staleCount: number;
  /**
   * Unimplemented findings that still warrant an implementation decision:
   * no child task, not a metadata-only finding, and not refuted by the
   * adversarial gate. This is the "needs attention" subset of proposedCount,
   * which otherwise lumps in metadata notes and refuted proposals.
   */
  actionableCount: number;
}

function computeFindingMetrics(
  findings: EnrichedFinding[],
  isRefuted?: (f: EnrichedFinding) => boolean,
): FindingMetrics {
  let mergedCount = 0, activeCount = 0, prCount = 0, failedCount = 0, proposedCount = 0, staleCount = 0, actionableCount = 0;
  for (const f of findings) {
    const status = f.outcome ? f.outcome.status : undefined;
    if (status === 'COMPLETED') mergedCount++;
    if (f.outcome && !['COMPLETED', 'FAILED', 'CANCELLED'].includes(f.outcome.status)) activeCount++;
    if (f.outcome?.prUrl) prCount++;
    if (f.outcome && ['FAILED', 'CANCELLED'].includes(f.outcome.status)) failedCount++;
    if (!f.outcome) {
      proposedCount++;
      if (getStaleness(f.taskCreatedAt) !== 'fresh') staleCount++;
      const isMeta = METADATA_FINDING_TYPES.has(f.finding.type ?? '');
      if (!isMeta && !(isRefuted?.(f) ?? false)) actionableCount++;
    }
  }
  return { mergedCount, activeCount, prCount, failedCount, proposedCount, staleCount, actionableCount };
}

const INCLUDED_STATUSES = new Set(['COMPLETED', 'IN_PROGRESS', 'EVALUATING']);

type SourceLink =
  | { kind: 'url'; href: string }
  | { kind: 'pr'; href: string; label: string }
  | { kind: 'path'; href: string; label: string }
  | { kind: 'text'; label: string };

function classifySourceRef(ref: string, repoUrl: string | null): SourceLink {
  if (/^https?:\/\//i.test(ref) && isSafeUrl(ref)) {
    return { kind: 'url', href: ref };
  }
  const prMatch = ref.match(/^pr[#-](\d+)$/i);
  if (prMatch && repoUrl) {
    const href = `${repoUrl.replace(/\/$/, '')}/pull/${prMatch[1]}`;
    if (isSafeUrl(href)) return { kind: 'pr', href, label: ref };
  }
  if (ref.includes('/') && repoUrl) {
    const m = ref.match(/^([\w./-]+?)(?::(\d+))?$/);
    if (m) {
      const [, path, line] = m;
      const href = `${repoUrl.replace(/\/$/, '')}/blob/HEAD/${path}${line ? `#L${line}` : ''}`;
      if (isSafeUrl(href)) return { kind: 'path', href, label: ref };
    }
  }
  return { kind: 'text', label: ref };
}

const AGREEMENT_STYLES: Record<string, { label: string; cls: string }> = {
  agrees: { label: 'Confirmed', cls: 'bg-emerald-900/40 text-emerald-400 border-emerald-700/40' },
  disagrees: { label: 'Challenged', cls: 'bg-red-900/40 text-red-400 border-red-700/40' },
  indeterminate: { label: 'Indeterminate', cls: 'bg-workbench-panel text-neutral-400 border-neutral-700/40' },
};

function ChallengePanel({ challenge, onNavigateToTask }: { challenge: FindingChallenge; onNavigateToTask?: (id: string) => void }) {
  const agreement = challenge.agreement ?? 'indeterminate';
  const style = AGREEMENT_STYLES[agreement] ?? AGREEMENT_STYLES.indeterminate;
  return (
    <div className="mt-1.5 border border-neutral-800 rounded p-2 space-y-1 bg-workbench-base/40">
      <div className="flex items-center gap-1.5 flex-wrap">
        <GitBranch className="w-3 h-3 text-neutral-500 shrink-0" />
        <span className="text-ui-sm uppercase tracking-wide text-neutral-500">Second opinion</span>
        <span className={`text-ui-sm border px-1.5 py-0.5 rounded font-medium ${style.cls}`}>{style.label}</span>
        {challenge.challenged_boundary && (
          <span className="text-ui-sm text-neutral-500">on <span className="font-mono text-neutral-400">{challenge.challenged_boundary}</span></span>
        )}
      </div>
      {challenge.second_opinion_verdict && (
        <div className="text-ui-sm text-neutral-400">
          <span className="text-neutral-600">Second verdict:</span> {challenge.second_opinion_verdict}
        </div>
      )}
      {challenge.reason && (
        <div className="text-ui-sm text-neutral-400 leading-relaxed">{challenge.reason}</div>
      )}
      {challenge.sidecar_task_id && (
        <div className="text-ui-sm text-neutral-500 flex items-center gap-1">
          <span>Challenger task:</span>
          {onNavigateToTask ? (
            <button
              data-viewport-allow-small-target
              onClick={e => { e.stopPropagation(); onNavigateToTask(challenge.sidecar_task_id ?? ''); }}
              className="font-mono text-cyan-500 hover:text-cyan-300 transition-colors"
            >
              {challenge.sidecar_task_id.slice(0, 8)}…
            </button>
          ) : (
            <span className="font-mono">{challenge.sidecar_task_id.slice(0, 8)}…</span>
          )}
        </div>
      )}
    </div>
  );
}

function EvidencePanel({ finding, repoUrl }: { finding: ResearchFinding; repoUrl: string | null }) {
  const [showAllClaims, setShowAllClaims] = useState(false);
  const claims = finding.claims ?? [];
  const sources = finding.sources ?? [];
  if (claims.length === 0 && sources.length === 0) return null;
  const visibleClaims = showAllClaims ? claims : claims.slice(0, 3);
  const hiddenCount = claims.length - 3;

  return (
    <div className="space-y-1.5 pt-1">
      {claims.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-0.5">Claims</div>
          <ul className="list-disc list-inside text-ui-md text-neutral-300 space-y-0.5">
            {visibleClaims.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
          {hiddenCount > 0 && (
            // reason: expand-claims link in compact finding card; text-ui-sm single-line height
            <button
              data-viewport-allow-small-target
              onClick={e => { e.stopPropagation(); setShowAllClaims(v => !v); }}
              className="mt-0.5 text-ui-sm text-cyan-400 hover:text-cyan-300 transition-colors"
            >
              {showAllClaims ? 'Show fewer' : `Show ${hiddenCount} more`}
            </button>
          )}
        </div>
      )}
      {sources.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-0.5">Sources</div>
          <ul className="space-y-0.5 text-ui-md">
            {sources.map((s, i) => {
              const link = classifySourceRef(s.ref, repoUrl);
              const refNode = link.kind === 'url' ? (
                <a
                  href={link.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-mono text-blue-400 hover:text-blue-300 break-all"
                  onClick={e => e.stopPropagation()}
                >{s.ref}</a>
              ) : (link.kind === 'pr' || link.kind === 'path') ? (
                <a
                  href={link.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-mono text-blue-400 hover:text-blue-300 break-all"
                  onClick={e => e.stopPropagation()}
                >{link.label}</a>
              ) : (
                <span className="font-mono text-neutral-300 break-all">{link.label}</span>
              );
              return (
                <li key={i} className="leading-snug">
                  {s.type && <span className="text-ui-sm text-neutral-500 mr-1">[{s.type}]</span>}
                  {refNode}
                  {s.excerpt && <div className="ml-3 italic text-ui-sm text-neutral-500">{s.excerpt}</div>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function OutcomeSummaryPills({ summary }: { summary: TaskEvaluationSummary }) {
  const highlights = summary.highlights ?? [];
  const { label: verdictLabel, tone: verdictToneId } = formatVerdict(summary.verdict);
  return (
    <div className="flex flex-wrap gap-1.5 text-ui-sm">
      <span className="rounded bg-workbench-panel/80 px-1.5 py-0.5 text-neutral-300 uppercase tracking-wide">
        {summary.status}
      </span>
      <span className={`rounded border px-1.5 py-0.5 uppercase tracking-wide ${verdictToneBadgeClass(verdictToneId)}`}>
        {verdictLabel}
      </span>
      {highlights.slice(0, 2).map((item, i) => (
        <span key={i} className="rounded bg-workbench-base/70 px-1.5 py-0.5 text-neutral-400 border border-neutral-800">
          <span className="text-neutral-500">{item.label}</span> {item.value}
        </span>
      ))}
    </div>
  );
}

type OutcomeFilter = 'all' | 'actionable' | 'shipped' | 'in_progress' | 'proposed' | 'failed' | 'findings_documented';

const IN_PROGRESS_OUTCOMES = new Set(['IN_PROGRESS', 'EVALUATING', 'QUEUED', 'PROVISIONING', 'BLOCKED', 'PARKED']);
const FAILED_OUTCOMES = new Set(['FAILED', 'CANCELLED']);

const METADATA_FINDING_TYPES = new Set(['design_doc_observation', 'direction_note', 'direction_proposal', 'direction_update']);

const OUTCOME_FILTER_OPTIONS: { value: OutcomeFilter; label: string; color: string }[] = [
  { value: 'all', label: 'All', color: 'text-neutral-300' },
  { value: 'actionable', label: 'Actionable', color: 'text-cyan-300' },
  { value: 'shipped', label: 'Shipped', color: 'text-emerald-400' },
  { value: 'in_progress', label: 'In Progress', color: 'text-cyan-400' },
  { value: 'proposed', label: 'Proposed', color: 'text-neutral-400' },
  { value: 'failed', label: 'Failed', color: 'text-red-400' },
  { value: 'findings_documented', label: 'Documented', color: 'text-violet-400' },
];

export function ResearchBrowser({ tasks, selectedProjectId, onClose, onNavigateToTask, initialDirectionId, researchFeed }: ResearchBrowserProps) {
  const [search, setSearch] = useState('');
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>('all');
  const [expandedGroups, setExpandedGroups] = useState<Set<string> | null>(null);
  const [expandedFinding, setExpandedFinding] = useState<string | null>(null);
  const [directions, setDirections] = useState<Direction[]>([]);
  const routedDirectionId = initialDirectionId ?? null;
  const [directionFilterState, setDirectionFilterState] = useState({
    routedDirectionId,
    value: routedDirectionId as string | null,
  });
  const directionFilter = directionFilterState.routedDirectionId === routedDirectionId
    ? directionFilterState.value
    : routedDirectionId;
  const setDirectionFilter = useCallback((value: string | null) => {
    setDirectionFilterState({ routedDirectionId, value });
  }, [routedDirectionId]);
  const [recentFindings, setRecentFindings] = useState<RecentFindingsResponse | null>(null);
  const [qualityCardCollapsed, setQualityCardCollapsed] = useState(false);
  const [gradeFilter, setGradeFilter] = useState<'HIGH' | 'MEDIUM' | 'LOW' | null>(null);

  // Fetch recent findings with actionability grades for the quality card
  useEffect(() => {
    if (!selectedProjectId) return;
    let cancelled = false;
    fetchRecentFindings(selectedProjectId)
      .then(data => { if (!cancelled) setRecentFindings(data); })
      .catch(() => {/* non-critical */});
    return () => { cancelled = true; };
  }, [selectedProjectId]);

  // Fetch directions for the selected project
  useEffect(() => {
    if (!selectedProjectId) return;
    let cancelled = false;
    (async () => {
      try {
        const h = await authHeaders();
        const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, { headers: h });
        if (res.ok && !cancelled) {
          const data = await res.json();
          setDirections(data.directions ?? data ?? []);
        }
      } catch { /* non-critical */ }
    })();
    return () => { cancelled = true; };
  }, [selectedProjectId]);

  const directionMap = useMemo(() => {
    const m = new Map<string, Direction>();
    for (const d of directions) m.set(d.id, d);
    return m;
  }, [directions]);

  // Derive repo URL from any task that has one — same project, so they share repo.
  const repoUrl = useMemo<string | null>(() => {
    for (const t of tasks) if (t.repo_url) return t.repo_url;
    return null;
  }, [tasks]);

  // Build the map of finding key -> child task info from the project-wide feed when available.
  const implementedFindings = useMemo(() => {
    const outcomeFeed = researchFeed ?? tasks;
    const map = new Map<string, { status: string; taskType?: string; taskId: string; prUrl?: string; failureClass?: string; evaluationSummary?: TaskEvaluationSummary | null; spawnGateState?: SpawnGateState }>();
    for (const task of outcomeFeed) {
      const ctx = task.context as Record<string, unknown> | undefined;
      if (ctx?.research_task_id && ctx?.finding_index !== undefined) {
        const key = `${ctx.research_task_id}-${ctx.finding_index}`;
        map.set(key, {
          status: task.status,
          taskType: task.task_type,
          taskId: task.id,
          prUrl: task.pr_url,
          failureClass: task.failure_class ?? undefined,
          evaluationSummary: task.evaluation_summary ?? undefined,
          spawnGateState: deriveSpawnGateState(task.context),
        });
      }
    }
    return map;
  }, [tasks, researchFeed]);

  // Build a lookup of adversarially-deferred findings, keyed BOTH by
  // (researchTaskId:hash:<finding_hash>) when a stable hash is present and by
  // (researchTaskId:title:<titleLower>) for compatibility. Lookups prefer the
  // hash key (stable across title edits) and fall back to title when either
  // side lacks a hash.
  const adversarialDeferredByKey = useMemo(() => {
    const feedTasks = researchFeed ?? tasks;
    const map = new Map<string, { adversarial_gate_reason: string | null }>();
    for (const task of feedTasks) {
      if (task.task_type !== 'research') continue;
      const deferred = task.context?.deferred_findings;
      if (!Array.isArray(deferred)) continue;
      for (const df of deferred) {
        const dfr = df as Record<string, unknown>;
        if (dfr.deferred_reason !== 'adversarial_gate') continue;
        const entry = {
          adversarial_gate_reason: (dfr.adversarial_gate_reason as string | null | undefined) ?? null,
        };
        const hash = dfr.finding_hash as string | undefined;
        if (hash) map.set(`${task.id}:hash:${hash}`, entry);
        const title = dfr.title as string | undefined;
        if (title) map.set(`${task.id}:title:${title.toLowerCase()}`, entry);
      }
    }
    return map;
  }, [tasks, researchFeed]);

  // True when an unimplemented finding was refuted by the adversarial gate on
  // its parent research task. Mirrors the badge lookup below: prefer the stable
  // finding_hash key, fall back to title. Used to keep refuted proposals out of
  // the "actionable" count and filter.
  const isFindingRefuted = useCallback((ef: EnrichedFinding): boolean => {
    if (ef.outcome) return false;
    const fh = ef.finding.finding_hash;
    const deferred =
      (fh ? adversarialDeferredByKey.get(`${ef.taskId}:hash:${fh}`) : undefined)
      ?? adversarialDeferredByKey.get(`${ef.taskId}:title:${ef.finding.title.toLowerCase()}`);
    return !!deferred;
  }, [adversarialDeferredByKey]);

  // Group findings by direction — prefer dedicated researchFeed over the paginated tasks prop.
  const groups = useMemo((): FindingGroup[] => {
    const byDirection = new Map<string, EnrichedFinding[]>();
    const feedTasks = researchFeed ?? tasks;

    for (const task of feedTasks) {
      if (task.task_type !== 'research' || !task.research_findings) continue;
      if (!INCLUDED_STATUSES.has(task.status)) continue;
      const findings: ResearchFinding[] = Array.isArray(task.research_findings) ? task.research_findings : [];

      for (let i = 0; i < findings.length; i++) {
        const f = findings[i];
        if (!f.title || !f.description) continue;

        const dirId = f.direction_id || '_undirected';
        const key = `${task.id}-${i}`;
        const outcome = implementedFindings.get(key);

        let bucket = byDirection.get(dirId);
        if (!bucket) { bucket = []; byDirection.set(dirId, bucket); }
        bucket.push({
          finding: f,
          taskId: task.id,
          taskCreatedAt: task.created_at,
          findingIndex: i,
          outcome,
        });
      }
    }

    // Sort findings within each group by date (newest first)
    for (const arr of byDirection.values()) {
      arr.sort((a, b) => parseTimestamp(b.taskCreatedAt).getTime() - parseTimestamp(a.taskCreatedAt).getTime());
    }

    // Build group objects
    const result: FindingGroup[] = [];
    for (const [dirId, findings] of byDirection) {
      const dir = dirId !== '_undirected' ? directionMap.get(dirId) : undefined;
      result.push({
        directionId: dirId === '_undirected' ? null : dirId,
        directionText: dir?.text ?? (dirId === '_undirected' ? 'Cross-cutting findings' : `Unknown direction (${dirId.slice(0, 12)})`),
        directionStatus: dir?.status,
        findings,
      });
    }

    // Sort groups: active directions first, then by finding count
    result.sort((a, b) => {
      const aActive = a.directionStatus === 'active' ? 0 : 1;
      const bActive = b.directionStatus === 'active' ? 0 : 1;
      if (aActive !== bActive) return aActive - bActive;
      return b.findings.length - a.findings.length;
    });

    return result;
  }, [tasks, researchFeed, implementedFindings, directionMap]);

  // Resolve direction name for the active filter chip
  const directionFilterText = useMemo(() => {
    if (!directionFilter) return null;
    const dir = directionMap.get(directionFilter);
    return dir?.text ?? `Direction ${directionFilter.slice(0, 8)}`;
  }, [directionFilter, directionMap]);

  // Filter by search and outcome
  const filteredGroups = useMemo(() => {
    const matchesOutcome = (f: EnrichedFinding): boolean => {
      const isMetadata = METADATA_FINDING_TYPES.has(f.finding.type ?? '');
      if (outcomeFilter === 'findings_documented') return isMetadata;
      // All non-documented filters exclude metadata findings (they have no implementation child).
      if (isMetadata) return false;
      if (outcomeFilter === 'all') return true;
      if (outcomeFilter === 'actionable') return !f.outcome && !isFindingRefuted(f);
      if (outcomeFilter === 'shipped') return f.outcome?.status === 'COMPLETED';
      if (outcomeFilter === 'in_progress') return !!f.outcome && IN_PROGRESS_OUTCOMES.has(f.outcome.status);
      if (outcomeFilter === 'proposed') return !f.outcome;
      if (outcomeFilter === 'failed') return !!f.outcome && FAILED_OUTCOMES.has(f.outcome.status);
      return true;
    };

    const q = search.trim().toLowerCase();
    return groups
      .filter(g => !directionFilter || g.directionId === directionFilter)
      .map(g => ({
        ...g,
        findings: g.findings.filter(f =>
          matchesOutcome(f) && (
            !q ||
            f.finding.title.toLowerCase().includes(q) ||
            f.finding.description.toLowerCase().includes(q) ||
            (f.finding.related_files ?? []).some(p => p.toLowerCase().includes(q))
          )
        ),
      }))
      .filter(g => g.findings.length > 0 || (outcomeFilter === 'all' && (!q || g.directionText.toLowerCase().includes(q))));
  }, [groups, search, outcomeFilter, directionFilter, isFindingRefuted]);

  const totalFindings = filteredGroups.reduce((sum, g) => sum + g.findings.length, 0);

  // Auto-expand first 3 groups until user interacts
  const effectiveExpanded = useMemo((): Set<string> => {
    if (expandedGroups !== null) return expandedGroups;
    return new Set(filteredGroups.slice(0, 3).map(g => g.directionId ?? '_undirected'));
  }, [expandedGroups, filteredGroups]);

  const groupMetrics = useMemo(
    () => new Map(filteredGroups.map(g => [g.directionId ?? '_undirected', computeFindingMetrics(g.findings, isFindingRefuted)])),
    [filteredGroups, isFindingRefuted]
  );

  const toggleGroup = (id: string) => {
    const next = new Set(effectiveExpanded);
    if (next.has(id)) next.delete(id); else next.add(id);
    setExpandedGroups(next);
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-[600px] bg-workbench-shell border-l border-neutral-800 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
          <div>
            <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
              <Compass className="w-4 h-4 text-cyan-400" />
              Findings journal
            </h3>
            <p className="text-ui-sm text-neutral-500 mt-0.5">
              {totalFindings} finding{totalFindings !== 1 ? 's' : ''} across {filteredGroups.length} direction{filteredGroups.length !== 1 ? 's' : ''}
              {outcomeFilter !== 'all' && (
                <span className="ml-1 text-neutral-600">— filtered by {OUTCOME_FILTER_OPTIONS.find(o => o.value === outcomeFilter)?.label.toLowerCase()}</span>
              )}
            </p>
          </div>
          {/* reason: icon-only close button in panel header; w-4 h-4 icon matches panel header density */}
          <button data-viewport-allow-small-target onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Search */}
        <div className="px-4 py-2 border-b border-neutral-800/50 shrink-0 space-y-2">
          {directionFilterText && (
            <div className="flex items-center gap-1.5">
              <span className="text-ui-sm text-neutral-500">Filtered:</span>
              <span className="flex items-center gap-1 text-ui-sm bg-cyan-950/40 border border-cyan-900/50 text-cyan-400 px-2 py-0.5 rounded max-w-[400px] truncate">
                <span className="truncate">{directionFilterText.length > 60 ? directionFilterText.slice(0, 60) + '…' : directionFilterText}</span>
                {/* reason: icon-only clear filter button inline in filter chip; w-2.5 h-2.5 icon */}
                <button data-viewport-allow-small-target onClick={() => setDirectionFilter(null)} className="ml-0.5 text-cyan-600 hover:text-cyan-300 transition-colors shrink-0">
                  <X className="w-2.5 h-2.5" />
                </button>
              </span>
            </div>
          )}
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-neutral-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search findings..."
              className="w-full bg-workbench-base border border-neutral-800 rounded pl-8 pr-3 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600 workbench-focus"
            />
          </div>
          <div className="flex items-center gap-1 flex-wrap">
            {OUTCOME_FILTER_OPTIONS.map(opt => (
              // reason: outcome filter chip in compact search toolbar; text-ui-sm py-0.5 matches search bar density
              <button
                data-viewport-allow-small-target
                key={opt.value}
                onClick={() => setOutcomeFilter(opt.value)}
                className={`px-2 py-0.5 rounded text-ui-sm font-medium transition-colors ${
                  outcomeFilter === opt.value
                    ? `bg-workbench-layer ${opt.color}`
                    : 'bg-workbench-base text-neutral-600 hover:text-neutral-400'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* Findings grouped by direction */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {/* Research quality card */}
          {recentFindings && recentFindings.total > 0 && (
            <div className="bg-workbench-base/50 border border-neutral-800 rounded-lg overflow-hidden">
              {/* reason: quality card collapse toggle; py-3 matches direction group header density */}
              <button
                data-viewport-allow-small-target
                onClick={() => setQualityCardCollapsed(c => !c)}
                className="w-full flex items-center gap-3 px-4 py-3 hover:bg-workbench-hover/30 transition-colors text-left"
              >
                <BarChart2 className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
                <span className="flex-1 text-xs font-medium text-neutral-200">Research quality</span>
                <span className="text-ui-sm text-neutral-500 shrink-0">{recentFindings.total} findings · last 7d</span>
                {qualityCardCollapsed
                  ? <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                  : <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                }
              </button>
              {!qualityCardCollapsed && (
                <div className="border-t border-neutral-800 px-4 py-3 space-y-3">
                  {/* Distribution bar */}
                  <div className="space-y-1.5">
                    <div className="flex h-2 rounded-full overflow-hidden bg-workbench-panel">
                      {recentFindings.grade_distribution_pct.HIGH > 0 && (
                        <div style={{ width: `${recentFindings.grade_distribution_pct.HIGH}%` }} className="bg-emerald-500" />
                      )}
                      {recentFindings.grade_distribution_pct.MEDIUM > 0 && (
                        <div style={{ width: `${recentFindings.grade_distribution_pct.MEDIUM}%` }} className="bg-amber-500" />
                      )}
                      {recentFindings.grade_distribution_pct.LOW > 0 && (
                        <div style={{ width: `${recentFindings.grade_distribution_pct.LOW}%` }} className="bg-workbench-card" />
                      )}
                    </div>
                    <div className="flex gap-4 text-ui-sm">
                      {/* reason: grade filter toggle chips; data-viewport-allow-small-target for sub-44px compact rows */}
                      <button
                        data-viewport-allow-small-target
                        onClick={() => setGradeFilter(g => g === 'HIGH' ? null : 'HIGH')}
                        className={`transition-colors ${gradeFilter === 'HIGH' ? 'text-emerald-300 underline' : 'text-emerald-400 hover:text-emerald-300'}`}
                        aria-pressed={gradeFilter === 'HIGH'}
                      >{recentFindings.grade_distribution.HIGH} HIGH ({recentFindings.grade_distribution_pct.HIGH}%)</button>
                      <button
                        data-viewport-allow-small-target
                        onClick={() => setGradeFilter(g => g === 'MEDIUM' ? null : 'MEDIUM')}
                        className={`transition-colors ${gradeFilter === 'MEDIUM' ? 'text-amber-300 underline' : 'text-amber-400 hover:text-amber-300'}`}
                        aria-pressed={gradeFilter === 'MEDIUM'}
                      >{recentFindings.grade_distribution.MEDIUM} MED ({recentFindings.grade_distribution_pct.MEDIUM}%)</button>
                      <button
                        data-viewport-allow-small-target
                        onClick={() => setGradeFilter(g => g === 'LOW' ? null : 'LOW')}
                        className={`transition-colors ${gradeFilter === 'LOW' ? 'text-neutral-300 underline' : 'text-neutral-500 hover:text-neutral-400'}`}
                        aria-pressed={gradeFilter === 'LOW'}
                      >{recentFindings.grade_distribution.LOW} LOW ({recentFindings.grade_distribution_pct.LOW}%)</button>
                    </div>
                  </div>
                  {/* Recent findings list */}
                  <div className="space-y-1">
                    {(gradeFilter ? recentFindings.findings.filter(f => f.actionability_grade === gradeFilter) : recentFindings.findings).slice(0, 10).map((f, i) => {
                      const handleFindingClick = () => {
                        // Find the matching EnrichedFinding in the grouped list and expand it
                        for (const group of groups) {
                          const match = group.findings.find(ef => ef.taskId === f.task_id && ef.finding.title === f.title);
                          if (match) {
                            const fKey = `${match.taskId}-${match.findingIndex}`;
                            setExpandedFinding(fKey);
                            const groupKey = match.finding.direction_id || '_undirected';
                            setExpandedGroups(prev => {
                              const next = new Set(prev ?? effectiveExpanded);
                              next.add(groupKey);
                              return next;
                            });
                            if (directionFilter && directionFilter !== (match.finding.direction_id ?? null)) {
                              setDirectionFilter(null);
                            }
                            break;
                          }
                        }
                      };
                      return (
                        <button
                          key={i}
                          onClick={handleFindingClick}
                          className="w-full flex items-center gap-2 text-ui-md text-left hover:bg-workbench-hover/30 px-1.5 py-1 rounded transition-colors min-h-[44px]"
                        >
                          <span className={`shrink-0 px-1.5 py-0.5 rounded text-ui-xs font-medium ${
                            f.actionability_grade === 'HIGH' ? 'bg-emerald-500/20 text-emerald-400' :
                            f.actionability_grade === 'LOW' ? 'bg-workbench-layer text-neutral-500' :
                            'bg-amber-500/20 text-amber-400'
                          }`}>
                            {f.actionability_grade}
                          </span>
                          <span className="text-neutral-300 truncate">{f.title}</span>
                          {f.researched_at && (
                            <span className="text-neutral-600 shrink-0 ml-auto">{relativeDate(f.researched_at)}</span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}

          {filteredGroups.length === 0 && (
            <div className="text-neutral-600 text-center py-12 text-sm">
              {groups.length === 0 ? 'No research findings yet.' : 'No findings match your search.'}
            </div>
          )}

          {filteredGroups.map(group => {
            const groupKey = group.directionId ?? '_undirected';
            const isExpanded = effectiveExpanded.has(groupKey);
            const { mergedCount, activeCount, prCount, failedCount, proposedCount, staleCount, actionableCount } =
              groupMetrics.get(groupKey) ?? computeFindingMetrics(group.findings, isFindingRefuted);

            return (
              <div key={groupKey} className="bg-workbench-base/50 border border-neutral-800 rounded-lg overflow-hidden">
                {/* reason: finding group expand row; py-3 renders at 44px but sub-pixel margin in compact results list */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => toggleGroup(groupKey)}
                  className="w-full flex items-center gap-3 px-4 py-3 hover:bg-workbench-hover/30 transition-colors text-left"
                >
                  {isExpanded
                    ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                    : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                  }
                  <div className="flex-1 min-w-0">
                    <span className="text-xs font-medium text-neutral-200 line-clamp-2 leading-relaxed">
                      {group.directionText.length > 120 ? group.directionText.slice(0, 120) + '...' : group.directionText}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 text-ui-sm">
                    {group.directionStatus && (
                      <span className={`px-1.5 py-0.5 rounded ${
                        group.directionStatus === 'active' ? 'bg-emerald-500/15 text-emerald-400' :
                        group.directionStatus === 'paused' ? 'bg-amber-500/15 text-amber-400' :
                        'bg-workbench-layer/50 text-neutral-500'
                      }`}>
                        {group.directionStatus}
                      </span>
                    )}
                    <span className="text-neutral-500 tabular-nums">
                      {group.findings.length}
                      {actionableCount > 0 && (
                        <span
                          className="text-cyan-300 ml-1 font-medium"
                          title="Unimplemented findings still awaiting an implementation decision (excludes metadata notes and refuted proposals)"
                        >
                          {actionableCount} actionable
                        </span>
                      )}
                      {mergedCount > 0 && <span className="text-emerald-500 ml-1">{mergedCount} merged</span>}
                      {activeCount > 0 && <span className="text-cyan-400 ml-1">{activeCount} active</span>}
                      {prCount > 0 && <span className="text-blue-400 ml-1">{prCount} PRs</span>}
                      {failedCount > 0 && <span className="text-red-400 ml-1">{failedCount} failed</span>}
                      {proposedCount > 0 && <span className="text-neutral-500 ml-1">{proposedCount} proposed</span>}
                      {staleCount > 0 && <span className="text-amber-400 ml-1">{staleCount} stale</span>}
                    </span>
                  </div>
                </button>

                {isExpanded && (
                  <div className="border-t border-neutral-800 divide-y divide-neutral-800/50">
                    {group.findings.map((ef) => {
                      const outcomeInfo = ef.outcome ? OUTCOME_STYLES[ef.outcome.status] : null;
                      const OutcomeIcon = outcomeInfo?.icon;
                      const outcomeTaskId = ef.outcome?.taskId;
                      const fKey = `${ef.taskId}-${ef.findingIndex}`;
                      const isDetailOpen = expandedFinding === fKey;
                      const toggleFindingDetail = () => setExpandedFinding(isDetailOpen ? null : fKey);
                      return (
                        // reason: finding expand row in research results; px-4 py-2 matches compact finding list density
                        <div
                          data-viewport-allow-small-target
                          role="button"
                          tabIndex={0}
                          key={fKey}
                          onClick={toggleFindingDetail}
                          onKeyDown={(event) => {
                            if (event.currentTarget !== event.target) return;
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            event.preventDefault();
                            toggleFindingDetail();
                          }}
                          className="w-full text-left px-4 py-2 hover:bg-workbench-hover/20 transition-colors"
                        >
                          <div className="flex items-center gap-2">
                            {outcomeInfo && OutcomeIcon && outcomeTaskId && onNavigateToTask
                              ? (
                                // reason: inline icon-only outcome indicator; w-3 h-3 matches finding row density
                                <button
                                  data-viewport-allow-small-target
                                  onClick={e => { e.stopPropagation(); onNavigateToTask(outcomeTaskId); }}
                                  className={`w-3 h-3 shrink-0 ${outcomeInfo.color} hover:opacity-70 transition-opacity`}
                                  title={`View task (${outcomeInfo.label})`}
                                >
                                  <OutcomeIcon className="w-3 h-3" />
                                </button>
                              )
                              : outcomeInfo && OutcomeIcon
                                ? <OutcomeIcon className={`w-3 h-3 shrink-0 ${outcomeInfo.color}`} />
                                : (() => {
                                  const s = getStaleness(ef.taskCreatedAt);
                                  return (
                                    <span className={`w-3 h-3 shrink-0 rounded-full border ${
                                      s === 'very_stale' ? 'border-red-500 bg-red-500/20' :
                                      s === 'stale' ? 'border-amber-500 bg-amber-500/20' :
                                      'border-neutral-700'
                                    }`} />
                                  );
                                })()
                            }
                            <span className="text-xs text-neutral-200 flex-1 truncate">{ef.finding.title}</span>
                            {ef.finding._provenance_warnings && ef.finding._provenance_warnings.broken_source_ref_count > 0 && (
                              <span
                                className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-400 text-ui-sm font-mono tabular-nums"
                                title={`Broken source refs:\n${ef.finding._provenance_warnings.broken_refs.slice(0, 5).join('\n')}`}
                              >
                                ⚠ {ef.finding._provenance_warnings.broken_source_ref_count} broken refs
                              </span>
                            )}
                            {ef.outcome?.spawnGateState?.kind === 'fail-open-pass' && (
                              <span
                                className="shrink-0 inline-flex items-center px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-400 text-ui-sm"
                                title={ef.outcome.spawnGateState.reason ?? 'Passed by fail-open — no challenger verdict was returned'}
                              >
                                fail-open
                              </span>
                            )}
                            {ef.outcome?.spawnGateState?.kind === 'challenged-pass' && (
                              <span
                                className="shrink-0 inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-400 text-ui-sm"
                                title={ef.outcome.spawnGateState.reason ?? 'Challenger confirmed this finding as actionable'}
                              >
                                gate ✓
                              </span>
                            )}
                            {!ef.outcome && (() => {
                              // Prefer hash-based matching when a finding_hash is
                              // available on the finding; fall back to title.
                              const fh = ef.finding.finding_hash;
                              const deferred =
                                (fh ? adversarialDeferredByKey.get(`${ef.taskId}:hash:${fh}`) : undefined)
                                ?? adversarialDeferredByKey.get(`${ef.taskId}:title:${ef.finding.title.toLowerCase()}`);
                              if (!deferred) return null;
                              return (
                                <span
                                  className="shrink-0 inline-flex items-center px-1.5 py-0.5 rounded bg-red-900/40 text-red-400 text-ui-sm"
                                  title={deferred.adversarial_gate_reason ?? 'Refuted by adversarial gate'}
                                >
                                  refuted
                                </span>
                              );
                            })()}
                            <span className="text-ui-sm text-neutral-600 shrink-0">{relativeDate(ef.taskCreatedAt)}</span>
                          </div>
                          {isDetailOpen && (
                            <div className="mt-2 ml-5 space-y-1.5" onClick={e => e.stopPropagation()}>
                              <p className="text-ui-md text-neutral-400 leading-relaxed">{ef.finding.description}</p>
                              <div className="flex items-center gap-3 flex-wrap text-ui-sm text-neutral-600">
                                {outcomeInfo && outcomeTaskId && onNavigateToTask
                                  ? (
                                    // reason: inline outcome label link in compact finding detail; text-ui-sm
                                    <button
                                      data-viewport-allow-small-target
                                      onClick={e => { e.stopPropagation(); onNavigateToTask(outcomeTaskId); }}
                                      className={`${outcomeInfo.color} hover:underline`}
                                    >
                                      {outcomeInfo.label}
                                    </button>
                                  )
                                  : outcomeInfo
                                    ? <span className={outcomeInfo.color}>{outcomeInfo.label}</span>
                                    : (() => {
                                        const s = getStaleness(ef.taskCreatedAt);
                                        const age = relativeDate(ef.taskCreatedAt);
                                        if (s === 'very_stale') return <span className="px-1.5 py-0.5 rounded bg-red-900/30 text-red-400">Stale · {age}</span>;
                                        if (s === 'stale') return <span className="px-1.5 py-0.5 rounded bg-amber-900/30 text-amber-400">Stale · {age}</span>;
                                        return <span className="text-neutral-500">Proposed · {age}</span>;
                                      })()
                                }
                                {ef.finding.related_files && ef.finding.related_files.length > 0 && (
                                  <span className="font-mono">{ef.finding.related_files.length} file{ef.finding.related_files.length !== 1 ? 's' : ''}</span>
                                )}
                                {ef.finding.scope && ef.finding.risk && (
                                  <span className={`inline-block text-ui-xs uppercase px-1 rounded ${
                                    ef.finding.risk === 'high' ? 'bg-red-900/40 text-red-400' :
                                    ef.finding.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' :
                                    'bg-emerald-900/40 text-emerald-400'
                                  }`}>{ef.finding.scope}/{ef.finding.risk}</span>
                                )}
                              {ef.finding.type && (
                                <span className="px-1 py-0.5 rounded bg-workbench-panel text-neutral-400">{ef.finding.type}</span>
                              )}
                              {ef.finding.priority && ef.finding.priority !== 'medium' && (
                                <span className={ef.finding.priority === 'high' ? 'text-amber-400' : 'text-neutral-500'}>{ef.finding.priority} priority</span>
                              )}
                            </div>
                              {ef.outcome?.evaluationSummary && (
                                <OutcomeSummaryPills summary={ef.outcome.evaluationSummary} />
                              )}
                              <EvidencePanel finding={ef.finding} repoUrl={repoUrl} />
                              {ef.finding.finding_challenge && (
                                <ChallengePanel challenge={ef.finding.finding_challenge} onNavigateToTask={onNavigateToTask} />
                              )}
                              {/* Downstream task chain */}
                              {ef.outcome && (
                                <div className="flex items-center gap-2 text-ui-sm text-neutral-500 mt-1">
                                  <span className="text-neutral-600">→</span>
                                  <TaskTypeBadge taskType={ef.outcome.taskType as import('../types/task').TaskType | undefined} />
                                  {ef.outcome.status === 'FAILED' && ef.outcome.failureClass && FAILURE_CLASS_CONFIG[ef.outcome.failureClass] && (
                                    <span className={`inline-block px-1.5 py-0.5 rounded text-ui-xs font-medium ${FAILURE_CLASS_CONFIG[ef.outcome.failureClass].color}`}>
                                      {FAILURE_CLASS_CONFIG[ef.outcome.failureClass].label}
                                    </span>
                                  )}
                                  {ef.outcome.prUrl && isSafeUrl(ef.outcome.prUrl) && (
                                    <a
                                      href={ef.outcome.prUrl}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="flex items-center gap-1 text-blue-400 hover:text-blue-300 transition-colors"
                                      onClick={e => e.stopPropagation()}
                                    >
                                      <ExternalLink className="w-3 h-3" />
                                      PR
                                    </a>
                                  )}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
