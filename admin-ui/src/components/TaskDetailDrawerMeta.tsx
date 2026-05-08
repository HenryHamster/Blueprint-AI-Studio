import { useState, useEffect, type ReactNode } from 'react';
import { Copy, Check, ChevronDown, ChevronRight, ExternalLink, Hash } from 'lucide-react';
import { getHeartbeatStatus, isSafeUrl, useElapsedTime, linkifyStatusMessage, ResearchFindings, FAILURE_CLASS_CONFIG, useDurationStats } from './TaskCard';
import type { Task } from './TaskCard';
import { formatAbsolute, parseTimestamp, timeAgo } from '../utils/time';
import { hasTransitiveCycle } from '../utils/taskGraph';
import { authGet, API_BASE_URL, fetchTaskSerialConsole } from '../api';
import type { SerialConsoleResponse } from '../types/task';
import { TaskFixLoopPanel } from './TaskFixLoopPanel';

function CopyableId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      className="inline-flex items-center gap-1.5 font-mono text-sm text-neutral-200 hover:text-white transition-colors group min-h-[44px]"
    >
      <span>{value}</span>
      {copied ? (
        <Check className="w-3.5 h-3.5 text-emerald-400" />
      ) : (
        <Copy className="w-3.5 h-3.5 text-neutral-500 group-hover:text-neutral-300 transition-colors" />
      )}
    </button>
  );
}

function SectionHeader({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5 first:mt-0">{children}</h3>;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">{label}</span>
      <div className="text-sm text-neutral-200 min-w-0">{children}</div>
    </div>
  );
}

function formatCreatedBy(value: string): string {
  if (value === 'USER') return 'User';
  if (value === 'ORCHESTRATOR') return 'Orchestrator';
  if (value.startsWith('AGENT_')) return `Agent (${value.slice(6)})`;
  return value;
}

function getLogEntryColor(msg: string): string {
  const lower = msg.toLowerCase();
  if (lower.includes('failed') || lower.includes('error') || lower.includes('cancelled')) return 'text-red-400';
  if (lower.includes('blocked') || lower.includes('conflict')) return 'text-amber-400';
  if (lower.includes('merged') || lower.includes('completed') || lower.includes('passed')) return 'text-emerald-400';
  if (msg.startsWith('Status changed:')) return 'text-blue-400';
  return 'text-neutral-300';
}


function DirectionsSection({ directions }: { directions: { text: string; priority?: string; status?: string }[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mt-5">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-neutral-400 hover:text-neutral-200 transition-colors w-full text-left mb-2"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Directions ({directions.length})
      </button>
      {open && (
        <ul className="space-y-1.5 pl-1">
          {directions.map((d, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-neutral-200">
              <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0" />
              <span className="break-words min-w-0">{d.text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface MetricsSnapshot {
  t: string;
  rss_mb?: number;
  heap_used_mb?: number;
  disk_free_mb?: number;
  disk_total_mb?: number;
  cgroup_mb?: number;
  cgroup_limit_mb?: number;
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const W = 40, H = 16, PAD = 1;
  const pts = values.map((v, i) => {
    const x = PAD + (i / (values.length - 1)) * (W - PAD * 2);
    const y = PAD + (1 - (v - min) / range) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const latest = values[values.length - 1];
  const pct = (latest - min) / range;
  const stroke = pct > 0.9 ? '#f87171' : pct > 0.7 ? '#fbbf24' : '#34d399';
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="inline-block align-middle">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

const DEP_STATUS_COLORS: Record<string, string> = { COMPLETED: 'text-emerald-400', IN_PROGRESS: 'text-amber-400', FAILED: 'text-red-400', CANCELLED: 'text-neutral-500', EVALUATING: 'text-blue-400', PROVISIONING: 'text-blue-400', QUEUED: 'text-neutral-400' };
function DependencyItem({ dep, depTask, hasCycle, onNavigateToTask }: { dep: string; depTask?: Task; hasCycle?: boolean; onNavigateToTask?: (id: string) => void }) {
  const statusColor = depTask ? (DEP_STATUS_COLORS[depTask.status] ?? 'text-neutral-400') : '';
  const label = depTask ? (depTask.title.length > 40 ? depTask.title.slice(0, 40) + '…' : depTask.title) : null;
  return (
    <div className="font-mono text-sm text-neutral-300 flex items-center gap-1.5 flex-wrap">
      <ChevronRight className="w-3 h-3 text-neutral-600 shrink-0" />
      {depTask ? (<>
        {onNavigateToTask
          ? <button onClick={() => onNavigateToTask(dep)} className="text-blue-400 hover:text-blue-300 transition-colors text-left">{label}</button>
          : <span>{label}</span>}
        <span className={`text-[10px] uppercase font-semibold ${statusColor}`}>{depTask.status}</span>
        {hasCycle && <span className="text-[10px] font-semibold text-orange-400">Cycle detected</span>}
      </>) : (<>
        <span className="text-neutral-500">{dep}</span>
        <span className="text-[10px] text-neutral-600">(not found)</span>
      </>)}
    </div>
  );
}
function ResearchOriginSection({ task, tasks, onNavigateToTask }: { task: Task; tasks?: Task[]; onNavigateToTask?: (taskId: string) => void }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const researchTaskId = ctx?.research_task_id as string | undefined;
  const findingIndex = ctx?.finding_index as number | undefined;
  const parentResearchTask = researchTaskId ? tasks?.find(t => t.id === researchTaskId) : undefined;
  const originFinding = parentResearchTask?.research_findings && findingIndex !== undefined
    ? parentResearchTask.research_findings[findingIndex]
    : undefined;
  if (!researchTaskId) return null;
  return (
    <>
      <SectionHeader>Research Origin</SectionHeader>
      {originFinding && <div className="text-xs text-neutral-300 mb-1 line-clamp-2">{originFinding.title}</div>}
      <DetailRow label="Research Task">
        {onNavigateToTask ? (
          <button onClick={() => onNavigateToTask(researchTaskId)} className="font-mono text-sm text-blue-400 hover:text-blue-300 hover:underline">{researchTaskId}</button>
        ) : (
          <CopyableId value={researchTaskId} />
        )}
      </DetailRow>
      {findingIndex !== undefined && <DetailRow label="Finding #"><span className="text-sm">#{findingIndex + 1}</span></DetailRow>}
      {originFinding?.scope && originFinding?.risk && (
        <DetailRow label="Scope/Risk">
          <span className={`text-xs px-1.5 py-0.5 rounded ${originFinding.risk === 'high' ? 'bg-red-900/40 text-red-400' : originFinding.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' : 'bg-emerald-900/40 text-emerald-400'}`}>{originFinding.scope}/{originFinding.risk}</span>
        </DetailRow>
      )}
      {originFinding?.direction_id && <DetailRow label="Direction"><span className="font-mono text-xs text-neutral-400">{originFinding.direction_id}</span></DetailRow>}
    </>
  );
}
interface ToolTraceEntry {
  tool?: string;
  name?: string;
  ts?: string;
  [key: string]: unknown;
}

function SerialLogBlock({ log, taskId, vmInstanceId }: { log: string | null; taskId: string; vmInstanceId?: string }) {
  const [copied, setCopied] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [liveResult, setLiveResult] = useState<SerialConsoleResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const displayLog = liveResult?.serial_log ?? log;
  const byteCount = displayLog ? new TextEncoder().encode(displayLog).length : 0;

  const handleCopy = () => {
    if (!displayLog) return;
    navigator.clipboard.writeText(displayLog);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleFetch = async () => {
    setFetching(true);
    setFetchError(null);
    try {
      const result = await fetchTaskSerialConsole(taskId);
      setLiveResult(result);
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : 'Fetch failed');
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="text-xs">
      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
        <span className="text-neutral-400 font-medium">Serial console</span>
        {displayLog && <span className="text-neutral-600">{byteCount.toLocaleString()} B</span>}
        {liveResult && (
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${liveResult.source === 'live' ? 'bg-emerald-900/40 text-emerald-400' : 'bg-neutral-800 text-neutral-400'}`}>
            {liveResult.source}
          </span>
        )}
        {liveResult?.vm_status && (
          <span className="text-neutral-500">{liveResult.vm_status}</span>
        )}
        {displayLog && (
          <button onClick={handleCopy} className="ml-auto flex items-center gap-1 text-neutral-500 hover:text-neutral-300 transition-colors">
            {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        )}
        {vmInstanceId && (
          <button
            onClick={handleFetch}
            disabled={fetching}
            className="flex items-center gap-1 text-blue-400 hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {fetching ? (
              <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : null}
            {fetching ? 'Fetching…' : 'Fetch current serial'}
          </button>
        )}
      </div>
      {fetchError && <p className="text-red-400 mb-1">{fetchError}</p>}
      {displayLog ? (
        <pre className="text-neutral-300 whitespace-pre-wrap break-words bg-neutral-950/60 rounded p-2 max-h-48 overflow-y-auto custom-scrollbar font-mono border border-neutral-800">{displayLog}</pre>
      ) : (
        <p className="text-neutral-600 italic">No serial log captured</p>
      )}
    </div>
  );
}

function ToolTraceTail({ entries }: { entries: ToolTraceEntry[] }) {
  const tail = entries.slice(-30);
  const [expandedSet, setExpandedSet] = useState<Set<number>>(new Set());
  const toggle = (i: number) => setExpandedSet(prev => {
    const next = new Set(prev);
    if (next.has(i)) { next.delete(i); } else { next.add(i); }
    return next;
  });
  return (
    <div className="space-y-0.5">
      {tail.map((entry, i) => {
        const label = entry.tool ?? entry.name ?? '(unknown)';
        const extra = Object.entries(entry).filter(([k]) => k !== 'tool' && k !== 'name' && k !== 'ts');
        const extraStr = extra.map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(' | ');
        const isLong = extraStr.length > 200;
        const isExpanded = expandedSet.has(i);
        return (
          <div key={i} className="font-mono text-[11px] text-neutral-400 leading-relaxed">
            <span className="text-blue-400">{label}</span>
            {entry.ts && <span className="text-neutral-600 ml-1.5">{String(entry.ts).slice(11, 19)}</span>}
            {extraStr && (
              <>
                <span className="text-neutral-600 ml-1.5">
                  {isLong && !isExpanded ? extraStr.slice(0, 200) + '…' : extraStr}
                </span>
                {isLong && (
                  <button onClick={() => toggle(i)} className="ml-1 text-neutral-500 hover:text-neutral-300 underline">{isExpanded ? 'less' : 'more'}</button>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatRetryAfter(val: unknown): string {
  if (typeof val !== 'string') return String(val);
  const d = parseTimestamp(val);
  const ms = d.getTime() - Date.now();
  if (ms > 0) {
    const sec = Math.round(ms / 1000);
    return sec < 60 ? `in ${sec}s` : `in ${Math.round(sec / 60)}m`;
  }
  return timeAgo(val);
}

function isHeartbeatStale(lastHeartbeat: string): boolean {
  return (Date.now() - parseTimestamp(lastHeartbeat).getTime()) > 300_000;
}

function DiagnosticsPanel({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const [open, setOpen] = useState(() => {
    if (task.status === 'FAILED') return true;
    if (!isTerminal && task.last_heartbeat) return isHeartbeatStale(task.last_heartbeat);
    return false;
  });

  const diagScalarFields = ['retry_after', 'provision_zone', 'provision_failures', 'max_plan_account_id', 'blacklisted_accounts', '_first_attempt_succeeded'];
  const hasScalar = diagScalarFields.some(f => f in (ctx ?? {}));
  const toolTrace = Array.isArray(ctx?._tool_trace) ? (ctx._tool_trace as ToolTraceEntry[]) : null;
  const serialLog = typeof ctx?.last_serial_log === 'string' ? ctx.last_serial_log : null;
  const hasAny = serialLog !== null || toolTrace !== null || hasScalar || task.vm_instance_id;
  if (!hasAny) return null;

  const c = ctx ?? {};

  return (
    <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 mb-2">
      {/* reason: collapsible section header; text-xs py-2 single-row height matches drawer density convention */}
      <button
        data-viewport-allow-small-target
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 w-full text-left px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400 hover:text-neutral-200 transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Diagnostics
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-neutral-800 pt-3">
          <SerialLogBlock
            log={serialLog}
            taskId={task.id}
            vmInstanceId={task.vm_instance_id}
          />

          {task.status_log && task.status_log.length > 0 && (
            <div className="text-xs">
              <p className="text-neutral-400 font-medium mb-1.5">Recent status log</p>
              <div className="space-y-0.5 max-h-32 overflow-y-auto custom-scrollbar bg-neutral-950/60 rounded p-2 border border-neutral-800">
                {task.status_log.slice(-20).reverse().map((entry, i) => (
                  <div key={i} className="flex items-start gap-2 font-mono">
                    <span className="text-neutral-600 shrink-0">{String(entry.t).slice(11, 19)}</span>
                    <span className={getLogEntryColor(entry.msg)}>{entry.msg}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {toolTrace && toolTrace.length > 0 && (
            <div className="text-xs">
              <p className="text-neutral-400 font-medium mb-1.5">Tool trace (last {Math.min(toolTrace.length, 30)} of {toolTrace.length})</p>
              <div className="bg-neutral-950/60 rounded p-2 max-h-40 overflow-y-auto custom-scrollbar border border-neutral-800">
                <ToolTraceTail entries={toolTrace} />
              </div>
            </div>
          )}

          {hasScalar && (
            <div className="space-y-1.5 text-xs">
              <p className="text-neutral-400 font-medium mb-1">Scalar fields</p>
              {'retry_after' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Retry after</span>
                  <span className="text-neutral-200">{formatRetryAfter(c.retry_after)}</span>
                </div>
              )}
              {'provision_zone' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Provision zone</span>
                  <span className="text-neutral-200 font-mono">{String(c.provision_zone)}</span>
                </div>
              )}
              {'provision_failures' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Provision attempts</span>
                  <span className="text-neutral-200">{String(c.provision_failures)}</span>
                </div>
              )}
              {'max_plan_account_id' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Account</span>
                  <span className="text-neutral-200 font-mono break-all">{String(c.max_plan_account_id)}</span>
                </div>
              )}
              {'blacklisted_accounts' in c && Array.isArray(c.blacklisted_accounts) && (c.blacklisted_accounts as unknown[]).length > 0 && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Blacklisted accts</span>
                  <span className="text-neutral-200 font-mono break-all">{(c.blacklisted_accounts as unknown[]).join(', ')}</span>
                </div>
              )}
              {'_first_attempt_succeeded' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">First attempt</span>
                  <span className={c._first_attempt_succeeded ? 'text-green-400' : 'text-red-400'}>{c._first_attempt_succeeded ? 'succeeded' : 'failed'}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface SelfReviewCheckResult {
  passed: boolean;
  issue?: string;
  reasoning?: string;
  severity?: string;
  evidence?: string;
}

interface ReviewerFinding {
  category: string;
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
  description: string;
}

interface EdgeConsidered {
  case: string;
  handled: boolean;
  evidence?: string;
  blind_spot?: string;
}

interface SelfReviewOutcome {
  verdict?: string;
  fixes_applied?: boolean;
  check_results?: Record<string, SelfReviewCheckResult>;
  blind_spots?: string[];
  reviewer_findings?: ReviewerFinding[];
  edges_considered?: EdgeConsidered[];
  fix_iteration?: number;
  _filtered_blind_spots_count?: number;
}

// Cross-reference: backend/app/sr_blind_spots.py DISALLOWED_BLIND_SPOT_EXAMPLES
const DISALLOWED_BLIND_SPOT_LABELS = [
  'full backend test suite',
  'docker pytest',
  'viewport tests',
];

function getSeverityColor(severity: string): string {
  switch (severity) {
    case 'CRITICAL': return 'bg-red-950/40 border-red-800/40 text-red-400';
    case 'HIGH': return 'bg-orange-950/40 border-orange-800/40 text-orange-400';
    case 'MEDIUM': return 'bg-amber-950/40 border-amber-800/40 text-amber-400';
    case 'LOW': return 'bg-yellow-950/40 border-yellow-800/40 text-yellow-400';
    default: return 'bg-neutral-800 border-neutral-700 text-neutral-400';
  }
}

function getSRPredictionBadge(category: string, checkResults: Record<string, SelfReviewCheckResult>): { icon: string; label: string; color: string } | null {
  if (!checkResults || !(category in checkResults)) {
    return { icon: '–', label: 'no SR signal', color: 'bg-neutral-900/40 border-neutral-700/40 text-neutral-500' };
  }
  const result = checkResults[category];
  if (!result.passed) {
    return { icon: '✓', label: 'caught by SR', color: 'bg-emerald-950/40 border-emerald-800/40 text-emerald-400' };
  }
  return { icon: '✗', label: 'missed by SR', color: 'bg-red-950/40 border-red-800/40 text-red-400' };
}

function CheckResultReasoningDisplay({ result }: { result: SelfReviewCheckResult }) {
  const [expanded, setExpanded] = useState(false);
  const reasoning = result.reasoning?.trim() ?? '';
  const hasReasoning = reasoning.length > 0;
  const isThinReasoning = hasReasoning && reasoning.length < 10;
  const shouldTruncate = reasoning.length > 200;
  const displayText = shouldTruncate && !expanded ? reasoning.slice(0, 200) + '…' : reasoning;

  if (!hasReasoning) return null;

  return (
    <div className={`text-xs ml-3.5 mt-1 break-words ${isThinReasoning ? 'italic text-neutral-500' : 'text-neutral-400'}`}>
      {displayText}
      {shouldTruncate && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="ml-1 text-neutral-500 hover:text-neutral-300 underline"
        >
          {expanded ? 'less' : 'more'}
        </button>
      )}
      {result.severity && (
        <span className={`ml-1.5 inline-block text-[9px] px-1.5 py-0.5 rounded font-semibold ${
          result.severity === 'CRITICAL' ? 'bg-red-950/40 text-red-400'
          : result.severity === 'HIGH' ? 'bg-orange-950/40 text-orange-400'
          : result.severity === 'MEDIUM' ? 'bg-yellow-950/40 text-yellow-400'
          : 'bg-neutral-800 text-neutral-400'
        }`}>
          {result.severity}
        </span>
      )}
    </div>
  );
}

function SelfReviewPanel({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const outcome = ctx?._self_review_outcome as SelfReviewOutcome | undefined;
  const humanCategories = ctx?._first_attempt_rejection_categories as Record<string, unknown> | undefined;
  const [open, setOpen] = useState(false);

  if (!outcome) return null;

  const verdict = outcome.verdict ?? 'unknown';
  const checkResults = outcome.check_results ?? {};
  const reviewerFindings = outcome.reviewer_findings ?? [];
  const isSoftFallback = verdict === 'unknown' && Object.keys(checkResults).length === 0;

  const verdictColor = verdict === 'pass' ? 'text-emerald-400' : verdict === 'fail' ? 'text-red-400' : 'text-neutral-400';
  const verdictBorderBg = verdict === 'pass' ? 'bg-emerald-950/30 border-emerald-800/40' : verdict === 'fail' ? 'bg-red-950/30 border-red-800/40' : 'bg-neutral-800 border-neutral-700';

  const failedCategories = Object.entries(checkResults).filter(([, r]) => !r.passed);
  const passedCategories = Object.entries(checkResults).filter(([, r]) => r.passed);

  const humanCats = humanCategories && Object.keys(humanCategories).length > 0 ? humanCategories : null;
  const allCategories = humanCats
    ? Array.from(new Set([...Object.keys(checkResults), ...Object.keys(humanCats)]))
    : [];
  const missedCount = humanCats
    ? allCategories.filter(cat => cat in humanCats && !(checkResults[cat] && !checkResults[cat].passed)).length
    : 0;

  return (
    <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 mb-2">
      {/* reason: collapsible section header; text-xs py-2 single-row height matches drawer density convention */}
      <button
        data-viewport-allow-small-target
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 w-full text-left px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400 hover:text-neutral-200 transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Self-Review
        <span className={`ml-auto text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded border ${verdictBorderBg} ${isSoftFallback ? 'text-neutral-500' : verdictColor}`}>
          {isSoftFallback ? 'no data' : verdict}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-neutral-800 pt-3">
          {isSoftFallback ? (
            <p className="text-xs text-neutral-500 italic">Soft-fallback envelope — no checks were recorded for this task.</p>
          ) : (
            <>
              <div className="flex items-center gap-4 flex-wrap text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="text-neutral-500">Verdict</span>
                  <span className={`font-semibold uppercase ${verdictColor}`}>{verdict}</span>
                </div>
                {outcome.fix_iteration !== undefined && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-neutral-500">Iteration</span>
                    <span className="font-semibold text-blue-400">#{outcome.fix_iteration}</span>
                  </div>
                )}
                {outcome.fixes_applied !== undefined && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-neutral-500">Fixes applied</span>
                    <span className={outcome.fixes_applied ? 'text-emerald-400' : 'text-neutral-400'}>
                      {outcome.fixes_applied ? 'yes' : 'no'}
                    </span>
                  </div>
                )}
              </div>

              {failedCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Failed checks</p>
                  <div className="space-y-1.5">
                    {failedCategories.map(([cat, result]) => {
                      const isFromReviewer = result.issue?.startsWith('[reviewer]');
                      return (
                        <div key={cat} className="text-xs">
                          <div className="flex items-center gap-1.5">
                            <span className={`w-2 h-2 rounded-full shrink-0 ${isFromReviewer ? 'bg-orange-500' : 'bg-red-500'}`} />
                            <span className={`font-mono ${isFromReviewer ? 'text-orange-300' : 'text-red-300'}`}>{cat}</span>
                            {isFromReviewer && <span className="text-[9px] px-1 py-0.5 rounded bg-orange-950/40 text-orange-300 font-semibold">from reviewer</span>}
                          </div>
                          {result.issue && <p className="text-neutral-400 ml-3.5 mt-0.5 break-words">{result.issue.startsWith('[reviewer]') ? result.issue.slice(10) : result.issue}</p>}
                          <CheckResultReasoningDisplay result={result} />
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {passedCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Passed checks</p>
                  <div className="space-y-1.5">
                    {passedCategories.map(([cat, result]) => {
                      const reasoning = result.reasoning?.trim() ?? '';
                      const isThinReasoning = reasoning.length > 0 && reasoning.length < 10;
                      return (
                        <div key={cat}>
                          <div className="flex items-center gap-1.5">
                            <span className={`w-2 h-2 rounded-full shrink-0 ${isThinReasoning ? 'bg-amber-500/60' : 'bg-emerald-500'}`} />
                            <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${
                              isThinReasoning
                                ? 'bg-amber-950/30 border-amber-800/30 text-amber-400'
                                : 'bg-emerald-950/30 border-emerald-800/30 text-emerald-400'
                            }`}>
                              {cat}
                            </span>
                          </div>
                          {reasoning && (
                            <CheckResultReasoningDisplay result={result} />
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {outcome.blind_spots && outcome.blind_spots.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Blind spots</p>
                  <ul className="space-y-1">
                    {outcome.blind_spots.map((spot, i) => (
                      <li key={i} className="text-xs text-neutral-400 flex items-start gap-1.5">
                        <span className="mt-1 w-1.5 h-1.5 rounded-full bg-amber-500/60 shrink-0" />
                        <span className="break-words">{spot}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(outcome._filtered_blind_spots_count ?? 0) > 0 && (
                <div className="bg-amber-950/20 border border-amber-800/30 rounded px-2.5 py-2">
                  <p className="text-xs text-amber-400">
                    Agent emitted {outcome._filtered_blind_spots_count} disallowed blind-spot pattern{outcome._filtered_blind_spots_count !== 1 ? 's' : ''} — filtered before aggregation
                  </p>
                  <p className="text-[10px] text-amber-500/70 mt-0.5">
                    Disallowed categories: {DISALLOWED_BLIND_SPOT_LABELS.join(', ')}
                  </p>
                </div>
              )}

              {outcome.edges_considered && outcome.edges_considered.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Edges considered ({outcome.edges_considered.length})</p>
                  <div className="rounded border border-neutral-700 overflow-hidden text-[10px]">
                    <div className="grid grid-cols-[1fr_auto] bg-neutral-800/60 px-2 py-1 font-semibold text-neutral-400 uppercase tracking-wide gap-2">
                      <span>Case</span>
                      <span>Handled</span>
                    </div>
                    {outcome.edges_considered.map((edge, i) => (
                      <div key={i} className={`px-2 py-1.5 border-t border-neutral-700/50 ${!edge.handled ? 'bg-amber-950/20' : ''}`}>
                        <div className="grid grid-cols-[1fr_auto] gap-2 items-start min-w-0">
                          <span className="text-neutral-300 break-words overflow-hidden">{edge.case}</span>
                          <span className={`font-semibold shrink-0 ${edge.handled ? 'text-emerald-400' : 'text-amber-400'}`}>
                            {edge.handled ? '✓ yes' : '✗ no'}
                          </span>
                        </div>
                        {edge.evidence && <p className="text-neutral-500 mt-0.5 break-words">evidence: {edge.evidence}</p>}
                        {edge.blind_spot && <p className="text-amber-400/70 mt-0.5 break-words">blind spot: {edge.blind_spot}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {reviewerFindings.length > 0 && (
                <div className="border-t border-neutral-700/50 pt-3">
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Fresh-reviewer findings</p>
                  <div className="space-y-2">
                    {reviewerFindings.sort((a, b) => {
                      const severityOrder: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
                      return (severityOrder[a.severity] ?? 4) - (severityOrder[b.severity] ?? 4);
                    }).map((finding, i) => {
                      const srBadge = getSRPredictionBadge(finding.category, checkResults);
                      return (
                        <div key={i} className={`text-xs rounded border px-2 py-1.5 ${getSeverityColor(finding.severity)}`}>
                          <div className="flex items-center gap-2 mb-0.5 flex-wrap">
                            <span className="font-semibold uppercase text-[9px] tracking-wide">{finding.severity}</span>
                            <span className="font-mono text-[10px]">{finding.category}</span>
                            {srBadge && (
                              <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded border inline-block ml-auto ${srBadge.color}`}>
                                {srBadge.icon} {srBadge.label}
                              </span>
                            )}
                          </div>
                          <p className="text-neutral-200 break-words">{finding.description}</p>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {humanCats && allCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Agent vs. Human review categories</p>
                  <div className="rounded border border-neutral-700 overflow-hidden text-[10px]">
                    <div className="grid grid-cols-3 bg-neutral-800/60 px-2 py-1 font-semibold text-neutral-400 uppercase tracking-wide">
                      <span>Category</span>
                      <span className="text-center">Agent</span>
                      <span className="text-center">Human</span>
                    </div>
                    {allCategories.map(cat => {
                      const agentResult = checkResults[cat];
                      const humanFlagged = humanCats != null && cat in humanCats;
                      const agentFailed = agentResult && !agentResult.passed;
                      const agentPassed = agentResult && agentResult.passed;
                      const missed = humanFlagged && !agentFailed;
                      return (
                        <div key={cat} className={`grid grid-cols-3 px-2 py-1.5 border-t border-neutral-700/50 ${missed ? 'bg-red-950/20' : ''}`}>
                          <span className="font-mono text-neutral-300 break-all">{cat}</span>
                          <span className="text-center">
                            {agentFailed ? <span className="text-red-400">✗ fail</span>
                              : agentPassed ? <span className="text-emerald-400">✓ pass</span>
                              : <span className="text-neutral-600">—</span>}
                          </span>
                          <span className="text-center">
                            {humanFlagged ? <span className="text-red-400">✗ flagged</span> : <span className="text-neutral-600">—</span>}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  {missedCount > 0 && (
                    <p className="text-[10px] text-amber-400 mt-1.5">
                      {missedCount} human-flagged {missedCount === 1 ? 'category' : 'categories'} not caught by agent
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function DetailsTab({ task, tasks, gcpProjectId, onNavigateToTask }: { task: Task; tasks?: Task[]; gcpProjectId?: string; onNavigateToTask?: (taskId: string) => void }) {
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const duration = useElapsedTime(task.started_at ?? '', isTerminal ? task.last_heartbeat : undefined);
  const durationStats = useDurationStats(task.project_id);
  const [now30, setNow30] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow30(Date.now()), 30000);
    return () => clearInterval(id);
  }, []);
  const [rawContextExpanded, setRawContextExpanded] = useState(false);
  const [directionsCache, setDirectionsCache] = useState<Array<{ id: string; text: string }>>([]);

  const provenanceCtx = task.context as Record<string, unknown> | undefined;
  const provenanceDirectionId = provenanceCtx?.direction_id as string | undefined;

  useEffect(() => {
    if (!provenanceDirectionId || !task.project_id) return;
    authGet(`${API_BASE_URL}/api/admin/projects/${task.project_id}/directions`)
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data.directions)) {
          setDirectionsCache(data.directions.map((d: Record<string, unknown>) => ({
            id: d.id as string,
            text: d.text as string,
          })));
        }
      })
      .catch(() => {});
  }, [task.project_id, provenanceDirectionId]);

  const prArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'PULL_REQUEST' && a.pr_url && isSafeUrl(a.pr_url));
  const commitArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'GIT_COMMIT');
  const investigationArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'INVESTIGATION_REPORT');
  // Group PR and commit artifacts by iteration for display
  const artifactIterations: { iteration: number; prs: typeof prArtifacts; commits: typeof commitArtifacts }[] = [];
  if (task.artifacts && task.artifacts.length > 0) {
    const iterSet = new Set<number>([
      ...prArtifacts.map(a => a.iteration ?? 1),
      ...commitArtifacts.map(a => a.iteration ?? 1),
    ]);
    Array.from(iterSet).sort((a, b) => a - b).forEach(iter => {
      artifactIterations.push({
        iteration: iter,
        prs: prArtifacts.filter(a => (a.iteration ?? 1) === iter),
        commits: commitArtifacts.filter(a => (a.iteration ?? 1) === iter),
      });
    });
  }
  const multiIteration = artifactIterations.length > 1;

  return (
    <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-1">
      <SectionHeader>Identifiers</SectionHeader>
      <DetailRow label="Task ID"><CopyableId value={task.id} /></DetailRow>
      {task.created_by && (
        <DetailRow label="Created By"><span className="text-sm">{formatCreatedBy(task.created_by)}</span></DetailRow>
      )}
      {task.parent_task_id && (() => {
        const parentId = task.parent_task_id;
        return (
        <DetailRow label="Parent Task">
          {onNavigateToTask ? (
            <button
              onClick={() => onNavigateToTask(parentId)}
              className="font-mono text-sm text-blue-400 hover:text-blue-300 hover:underline cursor-pointer"
            >
              {task.parent_task_id}
            </button>
          ) : (
            <CopyableId value={parentId} />
          )}
        </DetailRow>
        );
      })()}
      <ResearchOriginSection task={task} tasks={tasks} onNavigateToTask={onNavigateToTask} />
      {task.project_id && <DetailRow label="Project ID"><span className="font-mono text-sm">{task.project_id}</span></DetailRow>}
      {task.vm_instance_id && (
        <DetailRow label="VM Instance">
          <span className="inline-flex items-center gap-1.5">
            <span className="font-mono text-sm">{task.vm_instance_id}</span>
            <a
              href={gcpProjectId
                ? `https://console.cloud.google.com/compute/instances?project=${gcpProjectId}`
                : `https://console.cloud.google.com/compute/instances`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-gray-400 hover:text-gray-200 transition-colors"
              title="Open in GCP Console"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          </span>
        </DetailRow>
      )}

      {provenanceDirectionId && (() => {
        const direction = directionsCache.find(d => d.id === provenanceDirectionId);
        const directionText = direction?.text;
        const truncatedText = directionText && directionText.length > 80
          ? directionText.slice(0, 80) + '...'
          : directionText;
        return (
          <>
            <SectionHeader>Provenance</SectionHeader>
            <DetailRow label="Direction">
              <div className="min-w-0">
                {truncatedText && (
                  <span className="text-sm text-neutral-200 break-words block">{truncatedText}</span>
                )}
                <span className="text-xs text-neutral-500 font-mono">{provenanceDirectionId}</span>
              </div>
            </DetailRow>
          </>
        );
      })()}

      <SectionHeader>Timing</SectionHeader>
      <DetailRow label="Created at">{formatAbsolute(task.created_at)}</DetailRow>
      {task.started_at && <DetailRow label="Started at">{formatAbsolute(task.started_at)}</DetailRow>}
      {task.started_at && <DetailRow label="Duration"><span className="font-mono">{duration}</span></DetailRow>}
      {task.started_at && (task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING') && (() => {
        const taskType = task.task_type ?? ((task.payloads?.[0] as Record<string, unknown>)?.type as string) ?? 'create';
        const typeStat = durationStats[taskType];
        if (!typeStat || typeStat.sample_count < 3) return null;
        const elapsedSec = (now30 - parseTimestamp(task.started_at).getTime()) / 1000;
        const remaining = typeStat.p50_seconds - elapsedSec;
        const label = remaining > 0 ? `~${Math.ceil(remaining / 60)}m left` : 'longer than usual';
        const isOver = remaining <= 0;
        return (
          <DetailRow label="Est. remaining">
            <span className={`font-mono text-sm ${isOver ? 'text-amber-400' : 'text-neutral-200'}`} title={`Based on ${typeStat.sample_count} recent ${taskType} tasks`}>
              {label}
            </span>
          </DetailRow>
        );
      })()}
      <DetailRow label="Heartbeat">
        <span className="inline-flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${heartbeat.color}`} />
          <span className="text-sm">{heartbeat.text}</span>
          {task.last_heartbeat && (
            <span className="text-xs text-neutral-500">{formatAbsolute(task.last_heartbeat)}</span>
          )}
        </span>
      </DetailRow>
      {task.attempt_count != null && <DetailRow label="Attempts"><span>{task.attempt_count}</span></DetailRow>}

      {(() => {
        // R3-W2 (H1): read the dedicated column first; fall back to the
        // legacy ctx.last_metrics mirror only for old in-flight tasks
        // pre-deploy. Drop the fallback after one deploy cycle once no
        // in-flight tasks pre-date the column write.
        const metrics = (task.last_metrics_json ?? task.context?.last_metrics) as { rss_mb?: number; heap_used_mb?: number; disk_free_mb?: number; disk_total_mb?: number; cgroup_mb?: number; cgroup_limit_mb?: number } | undefined;
        const history = (task.context?.metrics_history ?? []) as MetricsSnapshot[];
        if (!metrics) return null;
        const useSparklines = history.length >= 2;
        const rssVals = useSparklines ? history.map(h => h.rss_mb).filter((v): v is number => v != null) : [];
        const heapVals = useSparklines ? history.map(h => h.heap_used_mb).filter((v): v is number => v != null) : [];
        const diskUsedVals = useSparklines
          ? history
              .map(h => (h.disk_total_mb != null && h.disk_free_mb != null) ? h.disk_total_mb - h.disk_free_mb : null)
              .filter((v): v is number => v != null)
          : [];
        const cgroupVals = useSparklines ? history.map(h => h.cgroup_mb).filter((v): v is number => v != null) : [];
        const diskTotal = metrics.disk_total_mb;
        const diskUsed = diskTotal != null && metrics.disk_free_mb != null ? diskTotal - metrics.disk_free_mb : null;
        const diskPct = diskUsed != null && diskTotal ? Math.round((diskUsed / diskTotal) * 100) : null;
        const cgroupColor = metrics.cgroup_mb != null && metrics.cgroup_limit_mb != null && metrics.cgroup_limit_mb > 0
          ? (metrics.cgroup_mb / metrics.cgroup_limit_mb) > 0.85 ? 'text-red-400'
          : (metrics.cgroup_mb / metrics.cgroup_limit_mb) > 0.70 ? 'text-amber-400'
          : 'text-green-400'
          : '';
        return (
          <>
            <SectionHeader>System Metrics</SectionHeader>
            {metrics.cgroup_mb != null && metrics.cgroup_limit_mb != null && (
              <DetailRow label="Container Memory">
                <span className="inline-flex items-center gap-2">
                  {cgroupVals.length >= 2 && <Sparkline values={cgroupVals} />}
                  <span className={`font-mono text-sm ${cgroupColor}`}>
                    {metrics.cgroup_mb} MB / {metrics.cgroup_limit_mb} MB ({Math.round(metrics.cgroup_mb / metrics.cgroup_limit_mb * 100)}%)
                  </span>
                </span>
              </DetailRow>
            )}
            {metrics.rss_mb != null && (
              <DetailRow label="RSS Memory">
                <span className="inline-flex items-center gap-2">
                  {rssVals.length >= 2 && <Sparkline values={rssVals} />}
                  <span className="font-mono text-sm">{metrics.rss_mb} MB</span>
                </span>
              </DetailRow>
            )}
            {metrics.heap_used_mb != null && (
              <DetailRow label="Heap Used">
                <span className="inline-flex items-center gap-2">
                  {heapVals.length >= 2 && <Sparkline values={heapVals} />}
                  <span className="font-mono text-sm">{metrics.heap_used_mb} MB</span>
                </span>
              </DetailRow>
            )}
            {diskUsed != null && diskTotal != null && (
              <DetailRow label="Disk (/tmp)">
                <span className="inline-flex items-center gap-2">
                  {diskUsedVals.length >= 2 && <Sparkline values={diskUsedVals} />}
                  <span className="font-mono text-sm">{diskUsed.toFixed(1)} MB used{diskPct != null ? ` (${diskPct}%)` : ''} / {diskTotal} MB</span>
                </span>
              </DetailRow>
            )}
          </>
        );
      })()}

      {task.description && <>
        <SectionHeader>Description</SectionHeader>
        <pre className="text-sm text-neutral-200 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800">{task.description}</pre>
      </>}

      {task.payloads && task.payloads.length > 0 && (() => {
        const firstPayload = task.payloads?.[0];
        if (!firstPayload) return null;
        const directions = firstPayload.directions as { text: string; priority?: string; status?: string }[] | undefined;
        if (!directions || directions.length === 0) return null;
        return <DirectionsSection directions={directions} />;
      })()}

      {task.payloads && task.payloads.length > 0 && (() => {
        const payload = task.payloads?.[0];
        if (!payload) return null;
        const prompt = payload.prompt as string | undefined;
        const baseBranch = payload.base_branch as string | undefined;
        const prNumber = payload.pr_number as number | undefined;
        const branch = payload.branch as string | undefined;
        const payloadRepoUrl = task.repo_url ?? commitArtifacts[0]?.repo_url;
        const prLink = prNumber && payloadRepoUrl ? `${payloadRepoUrl}/pull/${prNumber}` : undefined;
        return (
          <>
            <SectionHeader>Task Config</SectionHeader>
            {prompt && prompt !== task.description && <DetailRow label="Prompt"><pre className="text-sm text-neutral-200 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-32 overflow-y-auto custom-scrollbar border border-neutral-800">{prompt}</pre></DetailRow>}
            {baseBranch && <DetailRow label="Base Branch"><span className="font-mono text-sm">{baseBranch}</span></DetailRow>}
            {prNumber && (
              <DetailRow label="PR Number">
                {prLink && isSafeUrl(prLink) ? (
                  <a href={prLink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-mono text-sm text-blue-400 hover:text-blue-300 transition-colors">
                    #{prNumber}<ExternalLink className="w-3 h-3" />
                  </a>
                ) : (
                  <span className="font-mono text-sm">#{prNumber}</span>
                )}
              </DetailRow>
            )}
            {branch && <DetailRow label="Branch"><span className="font-mono text-sm">{branch}</span></DetailRow>}
          </>
        );
      })()}

      {task.plan_output && <>
        <SectionHeader>Plan Output</SectionHeader>
        <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-purple-950/20 rounded-lg p-3 max-h-64 overflow-y-auto custom-scrollbar border border-purple-900/30 font-mono">{JSON.stringify(task.plan_output, null, 2)}</pre>
      </>}

      {task.research_findings && <>
        <SectionHeader>Research Findings ({task.research_findings.length})</SectionHeader>
        <div className="bg-cyan-950/20 rounded-lg p-3 border border-cyan-900/30"><ResearchFindings findings={task.research_findings} /></div>
      </>}

      {(task.task_type === 'investigate' && task.plan_output) || investigationArtifacts.length > 0 ? (() => {
        const report = task.plan_output as {
          summary?: string;
          root_cause?: string;
          affected_files?: { path: string; issue: string }[];
          recommended_fix?: string;
          severity?: 'critical' | 'high' | 'medium' | 'low';
          confidence?: 'high' | 'medium' | 'low';
        } | null;
        if (!report) return null;

        const severityColors: Record<string, string> = {
          critical: 'text-red-400 bg-red-950/30 border-red-800/40',
          high: 'text-orange-400 bg-orange-950/30 border-orange-800/40',
          medium: 'text-yellow-400 bg-yellow-950/30 border-yellow-800/40',
          low: 'text-green-400 bg-green-950/30 border-green-800/40',
        };

        return (
          <>
            <SectionHeader>Investigation Report</SectionHeader>
            <div className="space-y-3 bg-amber-950/10 rounded-lg p-3 border border-amber-900/30">
              {report.severity && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Severity</span>
                  <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${severityColors[report.severity] ?? 'text-neutral-400'}`}>
                    {report.severity}
                  </span>
                </div>
              )}
              {report.confidence && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Confidence</span>
                  <span className="text-sm text-neutral-200 capitalize">{report.confidence}</span>
                </div>
              )}
              {report.summary && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Summary</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.summary}</p>
                </div>
              )}
              {report.root_cause && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Root Cause</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.root_cause}</p>
                </div>
              )}
              {report.recommended_fix && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Recommended Fix</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.recommended_fix}</p>
                </div>
              )}
              {report.affected_files && report.affected_files.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Affected Files</span>
                  <div className="min-w-0 space-y-1.5">
                    {report.affected_files.map((f, i) => (
                      <div key={i} className="text-sm">
                        <span className="font-mono text-blue-300 break-all">{f.path}</span>
                        {f.issue && <p className="text-neutral-400 text-xs mt-0.5 break-words">{f.issue}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        );
      })() : null}

      <DiagnosticsPanel task={task} />

      {task.context && Object.keys(task.context).length > 0 && (() => {
        function renderRetryEta(retryAfter: string | undefined) {
          if (!retryAfter) return null;
          const retryTime = parseTimestamp(retryAfter).getTime();
          const ms = retryTime - Date.now();
          if (ms > 0) {
            const mins = Math.ceil(ms / 60000);
            return <p className="text-xs text-emerald-400">Auto-retrying in {mins}m</p>;
          }
          return <p className="text-xs text-amber-400">Auto-retry pending</p>;
        }

        return (
          <>
            <SectionHeader>Context</SectionHeader>
            {task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class] && (
              <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 p-3 mb-2">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-semibold ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}>
                    {FAILURE_CLASS_CONFIG[task.failure_class].label}
                  </span>
                  <span className="text-xs text-neutral-400">{FAILURE_CLASS_CONFIG[task.failure_class].suggestion}</span>
                </div>
                {renderRetryEta(task.context?.retry_after as string | undefined)}
              </div>
            )}
            <div>
              {/* reason: inline expand toggle; text-xs single-line height for raw context section */}
              <button
                data-viewport-allow-small-target
                className="flex items-center gap-1 text-xs text-neutral-500 hover:text-neutral-300 mb-1"
                onClick={() => setRawContextExpanded(v => !v)}
              >
                {rawContextExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Raw Context
              </button>
              {rawContextExpanded && (
                <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono">
                  {JSON.stringify(task.context, null, 2)}
                </pre>
              )}
            </div>
          </>
        );
      })()}

      {task.error_logs && (
        <>
          <SectionHeader>Error Logs</SectionHeader>
          <pre className="text-sm text-red-300 whitespace-pre-wrap break-words bg-red-950/20 rounded-lg p-3 max-h-96 overflow-y-auto custom-scrollbar border border-red-900/30 font-mono">
            {task.error_logs}
          </pre>
        </>
      )}

      {task.status_log && task.status_log.length > 0 && (
        <>
          <SectionHeader>Status Log ({task.status_log.length})</SectionHeader>
          <div className="space-y-1 max-h-48 overflow-y-auto custom-scrollbar bg-neutral-900/50 rounded-lg p-3 border border-neutral-800">
            {task.status_log.slice().reverse().map((entry, i) => {
              const isStatusTransition = entry.msg.startsWith('Status changed:');
              return (
                <div key={i} className={`flex items-start gap-2 text-xs${isStatusTransition ? ' pl-2 border-l-2 border-blue-600/50' : ''}`}>
                  <span className="text-neutral-600 shrink-0 font-mono">{formatAbsolute(entry.t)}</span>
                  <span className={`${getLogEntryColor(entry.msg)} break-words min-w-0`}>{linkifyStatusMessage(entry.msg, task.pr_url)}</span>
                </div>
              );
            })}
          </div>
        </>
      )}

      <TaskFixLoopPanel
        taskId={task.id}
        fixLoopAbandoned={task.context?.fix_loop_abandoned}
      />

      <SelfReviewPanel task={task} />

      {task.history && task.history.length > 0 && (
        <>
          <SectionHeader>History</SectionHeader>
          <div className="space-y-2">
            {task.history.map((entry, i) => (
              <div key={i} className="flex items-start gap-2 text-sm">
                <div className="w-1.5 h-1.5 rounded-full bg-neutral-600 mt-1.5 shrink-0" />
                <div className="min-w-0">
                  <span className="text-neutral-500 text-xs">{formatAbsolute(entry.timestamp)}</span>
                  <span className="text-neutral-300 ml-2 font-medium">{entry.action}</span>
                  {entry.details && Object.keys(entry.details).length > 0 && (
                    <div className="text-xs text-neutral-500 mt-0.5 font-mono">
                      {JSON.stringify(entry.details)}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {task.artifacts && task.artifacts.length > 0 ? (
        <>
          {(prArtifacts.length > 0 || commitArtifacts.length > 0) && (
            <>
              <SectionHeader>Code Output</SectionHeader>
              <div className="space-y-3">
                {artifactIterations.map(({ iteration, prs, commits }) => (
                  <div key={iteration}>
                    {multiIteration && (
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="text-xs text-neutral-500 font-mono">Iteration {iteration}</span>
                        <div className="flex-1 border-t border-neutral-700" />
                      </div>
                    )}
                    <div className="space-y-1">
                      {prs.map((a, i) => a.pr_url ? (
                        <a
                          key={i}
                          href={a.pr_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
                        >
                          {a.pr_url}
                          <ExternalLink className="w-3.5 h-3.5" />
                        </a>
                      ) : null)}
                      {commits.map((a, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm font-mono text-neutral-300">
                          {a.commit_hash && <span className="text-emerald-400">{a.commit_hash.slice(0, 8)}</span>}
                          {a.repo_url && isSafeUrl(a.repo_url) ? (
                            <a href={a.repo_url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1">
                              {a.repo_url}<ExternalLink className="w-3 h-3" />
                            </a>
                          ) : a.repo_url ? (
                            <span className="text-neutral-400">{a.repo_url}</span>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      ) : (task.pr_url && isSafeUrl(task.pr_url) || task.commit_hash || task.repo_url && isSafeUrl(task.repo_url)) ? (
        <>
          <SectionHeader>Code Output</SectionHeader>
          <div className="space-y-2">
            {task.pr_url && isSafeUrl(task.pr_url) && (
              <a
                href={task.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                {task.pr_url}
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
            {task.commit_hash && (
              <div className="flex items-center gap-1.5">
                <Hash className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                <CopyableId value={task.commit_hash} />
              </div>
            )}
            {task.repo_url && isSafeUrl(task.repo_url) && !task.pr_url && (
              <a
                href={`${task.repo_url}/pulls`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                View Pull Requests
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
          </div>
        </>
      ) : (task.task_type === 'research' || task.task_type === 'plan') && task.repo_url && isSafeUrl(task.repo_url) ? (
        <>
          <SectionHeader>Repository</SectionHeader>
          <a
            href={`${task.repo_url}/pulls`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
          >
            View Pull Requests
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </>
      ) : null}

      {task.depends_on && task.depends_on.length > 0 && (
        <>
          <SectionHeader>Dependencies</SectionHeader>
          <div className="space-y-1">
            {task.depends_on.map((dep) => { const dt = tasks?.find(t => t.id === dep); return <DependencyItem key={dep} dep={dep} depTask={dt} hasCycle={hasTransitiveCycle(task.id, dep, tasks ?? [], 10)} onNavigateToTask={onNavigateToTask} />; })}
          </div>
        </>
      )}

      {task.child_task_ids && task.child_task_ids.length > 0 && (
        <>
          <SectionHeader>Child Tasks</SectionHeader>
          <div className="space-y-1">
            {task.child_task_ids.map((childId) => (
              <div key={childId} className="font-mono text-sm text-neutral-300 flex items-center gap-1.5">
                <ChevronRight className="w-3 h-3 text-neutral-600" />
                {onNavigateToTask ? (
                  <button
                    onClick={() => onNavigateToTask(childId)}
                    className="text-blue-400 hover:text-blue-300 transition-colors text-left"
                  >
                    {childId}
                  </button>
                ) : (
                  <CopyableId value={childId} />
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
