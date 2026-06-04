import { useState, useEffect, useMemo, type ReactNode } from 'react';
import { Copy, Check, ChevronDown, ChevronRight, ExternalLink, Hash } from 'lucide-react';
import { getHeartbeatStatus, isSafeUrl, useElapsedTime, linkifyStatusMessage, ResearchFindings, useDurationStats } from './TaskCard';
import { formatAbsolute, parseTimestamp } from '../utils/time';
import { hasTransitiveCycle } from '../utils/taskGraph';
import { authGet, API_BASE_URL, fetchComputeJobs, type ComputeJob } from '../api';
import { isComputeJobActive, computeJobTerminalLabel } from './ComputeJobsPanel.utils';
import { TaskFixLoopPanel } from './TaskFixLoopPanel';
import { SelfReviewPanel } from './TaskDetailDrawerSelfReview';
import { TaskDiagnosticsPanel, TaskErrorLogsPanel, getLogEntryColor } from './TaskDetailDrawerDiagnostics';
import { FailureDetailPanel } from './TaskDetailDrawerFailureDetail';
import type { Task, TaskEvaluationSummary } from '../types/task';

// Human-readable labels for the no-CI UNVERIFIED-merge reason keys. Mirrors
// backend _UNVERIFIED_REASON_LABELS (backend/app/alert_dispatch.py) — keep keys
// in sync. The orchestrator writes the reason KEY into
// review_loop["unverified_merge_reason"]; admin.py exposes it as
// task.unverified_merge_reason.
const UNVERIFIED_REASON_LABELS: Record<string, string> = {
  no_detectable_test_command: 'no runnable test command could be detected',
  kill_switch_off: 'the no-CI test-gate is disabled (kill-switch off)',
  internal_staging_base: 'the PR targets an internal staging branch (CI is path-filtered there)',
  undetected_runtime: 'the detected test suite produced no usable verdict',
};

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
      className="flex flex-wrap items-center gap-1.5 font-mono text-sm text-neutral-200 hover:text-white transition-colors group min-h-[44px] min-w-0"
    >
      <span className="break-all min-w-0">{value}</span>
      {copied ? (
        <Check className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
      ) : (
        <Copy className="w-3.5 h-3.5 text-neutral-500 group-hover:text-neutral-300 transition-colors shrink-0" />
      )}
    </button>
  );
}

function SectionHeader({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5 first:mt-0">{children}</h3>;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5 min-w-0">
      <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">{label}</span>
      <div className="text-sm text-neutral-200 min-w-0 flex-1">{children}</div>
    </div>
  );
}

function formatCreatedBy(value: string): string {
  if (value === 'USER') return 'User';
  if (value === 'ORCHESTRATOR') return 'Orchestrator';
  if (value.startsWith('AGENT_')) return `Agent (${value.slice(6)})`;
  return value;
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
/** Surface any compute jobs this task launched, linking the non-code compute
 *  work into the task detail view. Active jobs are managed (kill, live budget)
 *  from the dashboard's Compute Jobs panel; here we show a compact status link. */
function TaskComputeJobsSection({ task }: { task: Task }) {
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const projectId = task.project_id;
  const taskId = task.id;
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    fetchComputeJobs(projectId)
      .then(all => {
        if (!cancelled) setJobs(all.filter(j => j.task_id === taskId));
      })
      .catch(() => { /* compute jobs are optional context */ });
    return () => { cancelled = true; };
  }, [projectId, taskId]);

  if (jobs.length === 0) return null;
  return (
    <>
      <SectionHeader>Compute Jobs</SectionHeader>
      {jobs.map(job => {
        const active = isComputeJobActive(job);
        const cap = job.spend_cap_usd ?? 0;
        const spent = (active ? job.accrued_cost_usd : job.final_cost_usd ?? job.accrued_cost_usd) ?? 0;
        return (
          <div key={job.id} className="space-y-1">
            <DetailRow label={job.backend}>
              <span className="inline-flex flex-wrap items-center gap-1.5 min-w-0">
                <span className={`text-xs px-1.5 py-0.5 rounded ${active ? 'bg-sky-900/40 text-sky-300' : job.outcome === 'SUCCESS' ? 'bg-emerald-900/40 text-emerald-400' : 'bg-red-900/40 text-red-400'}`}>
                  {active ? job.status : computeJobTerminalLabel(job)}
                </span>
                <span className="text-xs text-neutral-400 font-mono break-words">
                  ${spent.toFixed(2)} / ${cap.toFixed(2)}
                </span>
              </span>
            </DetailRow>
            {job.result_reference && (
              <DetailRow label="Result">
                {isSafeUrl(job.result_reference) ? (
                  <a
                    href={job.result_reference}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1.5 text-sky-400 hover:text-sky-300 hover:underline break-all"
                  >
                    <span className="break-all">{job.result_reference}</span>
                    <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                  </a>
                ) : (
                  <span className="break-all text-neutral-300 font-mono text-xs">{job.result_reference}</span>
                )}
              </DetailRow>
            )}
          </div>
        );
      })}
    </>
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

function EvaluationSummarySection({ summary }: { summary: TaskEvaluationSummary }) {
  const evidence = summary.evidence ?? [];
  const highlights = summary.highlights ?? [];

  const badgeClass =
    summary.verdict === 'pass'
      ? 'bg-emerald-900/40 text-emerald-300 border-emerald-800/40'
      : summary.verdict === 'fail' || summary.verdict === 'actionable'
        ? 'bg-red-900/40 text-red-300 border-red-800/40'
        : summary.verdict === 'undetected'
          ? 'bg-neutral-800/50 text-neutral-300 border-neutral-700/50'
          : 'bg-cyan-900/40 text-cyan-300 border-cyan-800/40';

  return (
    <>
      <SectionHeader>Evaluation Summary</SectionHeader>
      <div className="space-y-3 bg-cyan-950/10 rounded-lg p-3 border border-cyan-900/30">
        <div className="flex items-start gap-2">
          <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Status</span>
          <span className="text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border bg-neutral-900/70 text-neutral-200 border-neutral-700/50">
            {summary.status}
          </span>
        </div>
        <div className="flex items-start gap-2">
          <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Verdict</span>
          <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${badgeClass}`}>
            {summary.verdict}
          </span>
        </div>
        {evidence.length > 0 && (
          <div className="flex items-start gap-2">
            <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Evidence</span>
            <div className="min-w-0 flex-1 space-y-1">
              {evidence.map((item, i) => (
                <div key={i} className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-neutral-300">
                  <span className="text-neutral-500 shrink-0">{item.label}</span>
                  <span className="break-words min-w-0">{item.value}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {highlights.length > 0 && (
          <div className="flex items-start gap-2">
            <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Highlights</span>
            <div className="min-w-0 flex-1 flex flex-wrap gap-1.5">
              {highlights.map((item, i) => (
                <span key={i} className="inline-flex max-w-full flex-wrap gap-x-1 rounded bg-neutral-900/70 px-2 py-1 text-[11px] text-neutral-200 border border-neutral-800">
                  <span className="text-neutral-500">{item.label}</span>
                  <span className="break-words">{item.value}</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </>
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
  const llmProvider = typeof provenanceCtx?.llm_provider === 'string' ? provenanceCtx.llm_provider : null;
  const llmModel = typeof provenanceCtx?.llm_model === 'string' ? provenanceCtx.llm_model : null;

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

  const { prArtifacts, commitArtifacts, investigationArtifacts, mergeTestVerdict, unverifiedMergeReason } = useMemo(() => {
    const artifacts = task.artifacts ?? [];
    // Surface the no-CI merge gate's verdict. The verify_tests handler posts a
    // MERGE_TEST_RESULT artifact whose `logs` carry the JSON verdict
    // ({detected, passed, tests, output}); the orchestrator consults it to
    // decide the merge. Parse the latest such artifact (highest iteration) and
    // map it to the same pass/fail/undetected outcome the backend gate uses
    // (classify_merge_test_verdict in ci_resolution.py): detected=false →
    // undetected; passed truthy → pass; otherwise → fail.
    const mergeTestArtifacts = artifacts.filter(a => a.artifact_type === 'MERGE_TEST_RESULT' && a.logs);
    let mergeTestVerdict: { outcome: 'pass' | 'fail' | 'undetected'; output: string } | null = null;
    if (mergeTestArtifacts.length > 0) {
      const latest = mergeTestArtifacts.reduce((a, b) => ((b.iteration ?? 1) >= (a.iteration ?? 1) ? b : a));
      try {
        const parsed = JSON.parse(latest.logs as string) as { detected?: boolean; passed?: boolean; output?: string };
        const outcome: 'pass' | 'fail' | 'undetected' = parsed.detected === false
          ? 'undetected'
          : parsed.passed
            ? 'pass'
            : 'fail';
        mergeTestVerdict = { outcome, output: typeof parsed.output === 'string' ? parsed.output : '' };
      } catch {
        mergeTestVerdict = null;
      }
    }
    return {
      prArtifacts: artifacts.filter(a => a.artifact_type === 'PULL_REQUEST' && a.pr_url && isSafeUrl(a.pr_url)),
      commitArtifacts: artifacts.filter(a => a.artifact_type === 'GIT_COMMIT'),
      investigationArtifacts: artifacts.filter(a => a.artifact_type === 'INVESTIGATION_REPORT'),
      mergeTestVerdict,
      unverifiedMergeReason: typeof task.unverified_merge_reason === 'string' ? task.unverified_merge_reason : null,
    };
  }, [task.artifacts, task.unverified_merge_reason]);
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
      <TaskComputeJobsSection task={task} />
      {task.project_id && <DetailRow label="Project ID"><span className="font-mono text-sm break-all min-w-0">{task.project_id}</span></DetailRow>}
      {task.vm_instance_id && (
        <DetailRow label="VM Instance">
          <span className="inline-flex items-center gap-1.5 flex-wrap min-w-0">
            <span className="font-mono text-sm break-all min-w-0">{task.vm_instance_id}</span>
            <a
              href={gcpProjectId
                ? `https://console.cloud.google.com/compute/instances?project=${gcpProjectId}`
                : `https://console.cloud.google.com/compute/instances`}
              target="_blank"
              rel="noopener noreferrer"
              data-viewport-allow-small-target
              className="text-gray-400 hover:text-gray-200 transition-colors shrink-0 p-1 -m-1"
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
      {task.api_call_summary != null && (() => {
        const s = task.api_call_summary;
        const calls = s.llm_call_count ?? 0;
        const input = s.total_input_tokens ?? 0;
        const output = s.total_output_tokens ?? 0;
        return (
          <DetailRow label="LLM Usage">
            <span className="font-mono text-sm">{calls.toLocaleString()} calls · {input.toLocaleString()} in / {output.toLocaleString()} out tokens</span>
          </DetailRow>
        );
      })()}
      {llmProvider && <DetailRow label="LLM Provider"><span className="font-mono text-sm break-all">{llmProvider}</span></DetailRow>}
      {llmModel && <DetailRow label="LLM Model"><span className="font-mono text-sm break-all">{llmModel}</span></DetailRow>}

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

      {(task.status === 'BLOCKED' || task.blocker_kind || task.blocked_reason || task.blocked_at || task.recheck_after) && (
        <>
          <SectionHeader>Blocker</SectionHeader>
          {task.blocker_kind && (
            <DetailRow label="Kind">
              <span className="text-xs uppercase tracking-wider font-mono bg-amber-500/10 text-amber-300 border border-amber-500/20 px-2 py-0.5 rounded">{task.blocker_kind}</span>
            </DetailRow>
          )}
          {task.blocked_reason && (
            <DetailRow label="Reason"><span className="text-sm text-amber-200/90 break-words">{task.blocked_reason}</span></DetailRow>
          )}
          {task.blocked_at && <DetailRow label="Blocked at">{formatAbsolute(task.blocked_at)}</DetailRow>}
          {task.recheck_after && (
            <DetailRow label="Recheck after">
              <span className="inline-flex items-center gap-2">
                <span>{formatAbsolute(task.recheck_after)}</span>
                {(() => {
                  const recheckTime = parseTimestamp(task.recheck_after).getTime();
                  if (recheckTime <= now30) return <span className="text-xs text-amber-400">recheck due</span>;
                  const mins = Math.max(1, Math.ceil((recheckTime - now30) / 60000));
                  return <span className="text-xs text-neutral-500 font-mono">in {mins}m</span>;
                })()}
              </span>
            </DetailRow>
          )}
        </>
      )}

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
            {baseBranch && <DetailRow label="Base Branch"><span className="font-mono text-sm break-all min-w-0">{baseBranch}</span></DetailRow>}
            {prNumber && (
              <DetailRow label="PR Number">
                {prLink && isSafeUrl(prLink) ? (
                  <a href={prLink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-mono text-sm text-blue-400 hover:text-blue-300 transition-colors min-h-[44px] px-1.5">
                    #{prNumber}<ExternalLink className="w-3 h-3 shrink-0" />
                  </a>
                ) : (
                  <span className="font-mono text-sm">#{prNumber}</span>
                )}
              </DetailRow>
            )}
            {branch && <DetailRow label="Branch"><span className="font-mono text-sm break-all min-w-0">{branch}</span></DetailRow>}
          </>
        );
      })()}

      {task.evaluation_summary && <EvaluationSummarySection summary={task.evaluation_summary} />}

      {task.plan_output && (() => {
        const planOut = task.plan_output as Record<string, unknown> | undefined;
        const memoData = planOut?.memo as Record<string, unknown> | undefined;
        return typeof memoData === 'object' && memoData !== null;
      })() ? (() => {
        const planOut = task.plan_output as Record<string, unknown>;
        const memo = planOut.memo as {
          summary?: string;
          reasoning?: string;
          citations?: string[];
          tags?: string[];
        } | undefined;
        if (!memo) return null;

        const renderCitationLabel = (citation: string): string => {
          if (citation.startsWith('task:')) {
            return citation.slice(5);
          }
          if (citation.startsWith('direction:')) {
            return citation;
          }
          return citation;
        };

        const getCitationType = (citation: string): 'task' | 'direction' | 'other' => {
          if (citation.startsWith('task:')) return 'task';
          if (citation.startsWith('direction:')) return 'direction';
          return 'other';
        };

        return (
          <>
            <SectionHeader>Memo Reasoning</SectionHeader>
            <div className="space-y-3 bg-blue-950/10 rounded-lg p-3 border border-blue-900/30">
              {memo.summary && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Summary</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{memo.summary}</p>
                </div>
              )}
              {memo.reasoning && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Reasoning</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words whitespace-pre-wrap">{memo.reasoning}</p>
                </div>
              )}
              {memo.tags && memo.tags.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Tags</span>
                  <div className="flex flex-wrap gap-1.5 min-w-0">
                    {memo.tags.map((tag, i) => (
                      <span key={i} className="inline-block text-xs px-2 py-0.5 rounded bg-blue-900/40 text-blue-300 border border-blue-800/40">
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {memo.citations && memo.citations.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Citations</span>
                  <div className="space-y-1 min-w-0 flex-1">
                    {memo.citations.map((citation, i) => {
                      const type = getCitationType(citation);
                      const label = renderCitationLabel(citation);
                      return (
                        <div key={i} className="text-sm">
                          {type === 'task' ? (
                            onNavigateToTask ? (
                              <button
                                onClick={() => onNavigateToTask(label)}
                                className="text-blue-400 hover:text-blue-300 transition-colors font-mono break-all text-left"
                              >
                                {label}
                              </button>
                            ) : (
                              <span className="text-blue-300 font-mono break-all">{label}</span>
                            )
                          ) : (
                            <span className="text-neutral-300 break-all">{citation}</span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </>
        );
      })() : null}

      {task.plan_output && task.task_type !== 'memo' && <>
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

      {(mergeTestVerdict || unverifiedMergeReason) && (() => {
        const config: Record<'pass' | 'fail' | 'undetected' | 'unverified', { label: string; badge: string; note: string }> = {
          pass: {
            label: 'Passed',
            badge: 'text-emerald-400 bg-emerald-950/30 border-emerald-800/40',
            note: "The repository's own test suite ran on the PR head and passed — it served as the merge gate.",
          },
          fail: {
            label: 'Failed',
            badge: 'text-red-400 bg-red-950/30 border-red-800/40',
            note: "The repository's own test suite ran on the PR head and failed — the merge gate blocked the merge.",
          },
          undetected: {
            label: 'No suite detected',
            badge: 'text-neutral-400 bg-neutral-800/40 border-neutral-700/40',
            note: 'No runnable test suite was detected on the PR head — the gate fell back to proceeding unverified.',
          },
          unverified: {
            label: 'Merged unverified',
            badge: 'text-amber-400 bg-amber-950/30 border-amber-800/40',
            // Reason appended below; this note is the generic fallback.
            note: 'The merge bypassed test verification.',
          },
        };
        // The persisted unverified-merge reason is an independent signal that
        // opens this section and selects the UNVERIFIED verdict. When both an
        // artifact verdict and a reason are present, the reason takes precedence
        // for the verdict label/note while any captured test output still shows.
        const outcome: 'pass' | 'fail' | 'undetected' | 'unverified' =
          unverifiedMergeReason || !mergeTestVerdict ? 'unverified' : mergeTestVerdict.outcome;
        const v = config[outcome];
        const reasonLabel = unverifiedMergeReason
          ? (UNVERIFIED_REASON_LABELS[unverifiedMergeReason] ?? unverifiedMergeReason)
          : null;
        const note = reasonLabel
          ? `The merge bypassed test verification — ${reasonLabel}.`
          : v.note;
        return (
          <>
            <SectionHeader>Merge Test Gate</SectionHeader>
            <div className="space-y-3 bg-emerald-950/10 rounded-lg p-3 border border-emerald-900/30">
              <div className="flex items-center gap-2">
                <span className="text-xs text-neutral-500 w-28 shrink-0">Verdict</span>
                <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${v.badge}`}>
                  {v.label}
                </span>
              </div>
              <p className="text-xs text-neutral-400 break-words">{note}</p>
              {mergeTestVerdict?.output && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Test Output</span>
                  <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono min-w-0 flex-1">{mergeTestVerdict.output}</pre>
                </div>
              )}
            </div>
          </>
        );
      })()}

      <TaskDiagnosticsPanel task={task} />

      {task.context && Object.keys(task.context).length > 0 && (
          <>
            <SectionHeader>Context</SectionHeader>
            <FailureDetailPanel task={task} />
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
      )}

      {task.error_logs && (
        <>
          <SectionHeader>Error Logs</SectionHeader>
          <TaskErrorLogsPanel errorLogs={task.error_logs} />
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
                          className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors break-all min-w-0 min-h-[44px]"
                        >
                          {a.pr_url}
                          <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                        </a>
                      ) : null)}
                      {commits.map((a, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm font-mono text-neutral-300 flex-wrap min-w-0">
                          {a.commit_hash && <span className="text-emerald-400">{a.commit_hash.slice(0, 8)}</span>}
                          {a.repo_url && isSafeUrl(a.repo_url) ? (
                            <a href={a.repo_url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1 break-all min-w-0 min-h-[44px]">
                              {a.repo_url}<ExternalLink className="w-3 h-3 shrink-0" />
                            </a>
                          ) : a.repo_url ? (
                            <span className="text-neutral-400 break-all min-w-0">{a.repo_url}</span>
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
                className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors break-all min-w-0 min-h-[44px]"
              >
                {task.pr_url}
                <ExternalLink className="w-3.5 h-3.5 shrink-0" />
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
                className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors min-h-[44px]"
              >
                View Pull Requests
                <ExternalLink className="w-3.5 h-3.5 shrink-0" />
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
            className="flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors min-h-[44px]"
          >
            View Pull Requests
            <ExternalLink className="w-3.5 h-3.5 shrink-0" />
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
              <div key={childId} className="font-mono text-sm text-neutral-300 flex items-center gap-1.5 flex-wrap min-w-0">
                <ChevronRight className="w-3 h-3 text-neutral-600 shrink-0" />
                {onNavigateToTask ? (
                  <button
                    onClick={() => onNavigateToTask(childId)}
                    className="text-blue-400 hover:text-blue-300 transition-colors text-left break-all min-w-0"
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
