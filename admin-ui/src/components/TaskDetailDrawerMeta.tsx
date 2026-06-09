import { useState, useEffect, useMemo, type ReactNode } from 'react';
import { Copy, Check, ChevronDown, ChevronRight, ExternalLink, Hash, BookOpen, CheckCircle, XCircle, HelpCircle } from 'lucide-react';
import { getHeartbeatStatus, isSafeUrl, ResolvedReferenceLink, useElapsedTime, linkifyStatusMessage, ResearchFindings, useDurationStats } from './TaskCard';
import { MarkdownContent } from './MarkdownContent';
import { formatAbsolute, parseTimestamp, timeAgo } from '../utils/time';
import { formatVerdict, verdictToneBadgeClass } from '../utils/verdictFormatting';
import { hasTransitiveCycle } from '../utils/taskGraph';
import { authGet, API_BASE_URL, fetchComputeJobs, fetchKnowledgeDocument, type ComputeJob } from '../api';
import { isComputeJobActive, computeJobTerminalLabel } from './ComputeJobsPanel.utils';
import { TaskFixLoopPanel } from './TaskFixLoopPanel';
import { SelfReviewPanel } from './TaskDetailDrawerSelfReview';
import { TaskDiagnosticsPanel, TaskErrorLogsPanel, getLogEntryColor } from './TaskDetailDrawerDiagnostics';
import { FailureDetailPanel, TeardownForensicsPanel } from './TaskDetailDrawerFailureDetail';
import type { PanelReviewIssue, PanelReviewVerdict, Task, TaskEvaluationSummary } from '../types/task';
import { deriveSpawnGateState } from '../types/task';
import { humanizeUnverifiedReason, labelNoTestsReason } from '../utils/unverifiedReasonLabels';
import {
  parseMergeTestVerdict,
  deriveMergeTestOutcome,
  type MergeTestVerdict,
  type MergeTestResult,
} from '../utils/mergeTestVerdict';

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

function formatArtifactTypeLabel(artifactType: string): string {
  return artifactType
    .toLowerCase()
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function formatWakeDeliveryState(value: string): string {
  return value
    .toLowerCase()
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
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
/** Classify whether a terminal kill was intentional (budget cap / deadline /
 *  operator cancel) vs a genuine crash — drives amber vs red badge styling. */
function isIntentionalKill(job: ComputeJob): boolean {
  if (job.outcome === 'SPEND_CAP_EXCEEDED' || job.outcome === 'TIMED_OUT') return true;
  if (job.outcome === 'CANCELLED' && (job.terminal_reason ?? '').toLowerCase().includes('operator')) return true;
  return false;
}

function fmtDeadlineSeconds(s: number): string {
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${s}s`;
}

/** Surface any compute jobs this task launched, linking the non-code compute
 *  work into the task detail view. For compute_experiment tasks the launch
 *  contract (command, spend cap, deadline, success criterion) is shown above
 *  the per-job rows. Intentional kills (budget/deadline/operator) are styled
 *  amber to distinguish them from genuine crashes (red). */
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

  const isComputeExperiment = (task.task_type as string) === 'compute_experiment' ||
    (task.payloads?.[0] as Record<string, unknown> | undefined)?.type === 'compute_experiment';
  const firstPayload = isComputeExperiment
    ? (task.payloads?.[0] as Record<string, unknown> | undefined)
    : undefined;
  const contractCommand = firstPayload?.command as string | undefined;
  const contractSpendCap = firstPayload?.spend_cap_usd as number | undefined;
  const contractDeadline = firstPayload?.deadline_seconds as number | undefined;
  const contractCriterion = firstPayload?.success_criterion as
    | { metric: string; operator: string; threshold: number }
    | undefined;

  return (
    <>
      <SectionHeader>Compute Jobs</SectionHeader>
      {isComputeExperiment && (contractCommand || contractSpendCap != null || contractDeadline != null) && (
        <div className="mb-3 rounded-lg border border-sky-900/30 bg-sky-950/10 p-3 space-y-2">
          <div className="text-xs font-semibold text-sky-400 uppercase tracking-wider mb-1">Launch Contract</div>
          {contractCommand && (
            <div className="flex items-start gap-2">
              <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Command</span>
              <pre className="text-xs text-neutral-200 font-mono whitespace-pre-wrap break-words min-w-0 flex-1">{contractCommand}</pre>
            </div>
          )}
          {contractSpendCap != null && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-neutral-500 w-28 shrink-0">Spend cap</span>
              <span className="text-sm font-mono text-neutral-200">${contractSpendCap.toFixed(2)}</span>
            </div>
          )}
          {contractDeadline != null && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-neutral-500 w-28 shrink-0">Deadline</span>
              <span className="text-sm font-mono text-neutral-200">{fmtDeadlineSeconds(contractDeadline)}</span>
            </div>
          )}
          {contractCriterion && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-neutral-500 w-28 shrink-0">Success criterion</span>
              <span className="text-sm font-mono text-neutral-200">
                {contractCriterion.metric} {contractCriterion.operator} {contractCriterion.threshold}
              </span>
            </div>
          )}
        </div>
      )}
      {jobs.map(job => {
        const active = isComputeJobActive(job);
        const cap = job.spend_cap_usd ?? 0;
        const spent = (active ? job.accrued_cost_usd : job.final_cost_usd ?? job.accrued_cost_usd) ?? 0;
        const terminalLabel = computeJobTerminalLabel(job);
        const intentional = !active && isIntentionalKill(job);
        const badgeClass = active
          ? 'bg-sky-900/40 text-sky-300'
          : job.outcome === 'SUCCESS'
            ? 'bg-emerald-900/40 text-emerald-400'
            : intentional
              ? 'bg-amber-900/40 text-amber-400'
              : job.outcome === 'FAILURE'
                ? 'bg-red-900/40 text-red-400'
                : 'bg-neutral-800/60 text-neutral-400';
        return (
          <div key={job.id} className="space-y-1 mb-2">
            <DetailRow label={job.backend}>
              <span className="inline-flex flex-wrap items-center gap-1.5 min-w-0">
                <span className={`text-xs px-1.5 py-0.5 rounded ${badgeClass}`}>
                  {active ? job.status : terminalLabel}
                </span>
                <span className="text-xs text-neutral-400 font-mono break-words">
                  ${spent.toFixed(2)} / ${cap.toFixed(2)}
                </span>
              </span>
            </DetailRow>
            {isComputeExperiment && job.machine_type && (
              <DetailRow label="Machine">
                <span className="font-mono text-xs text-neutral-300">{job.machine_type}</span>
              </DetailRow>
            )}
            {isComputeExperiment && job.accelerator_type && (
              <DetailRow label="Accelerator">
                <span className="font-mono text-xs text-neutral-300">
                  {job.accelerator_count != null && job.accelerator_count > 1 ? `${job.accelerator_count}× ` : ''}{job.accelerator_type}
                </span>
              </DetailRow>
            )}
            {!active && job.terminal_reason && (
              <DetailRow label="Terminal reason">
                <span className="text-xs text-neutral-400 break-words">{job.terminal_reason}</span>
              </DetailRow>
            )}
            {job.result_reference && (
              <DetailRow label="Artifact">
                <ResolvedReferenceLink
                  projectId={projectId}
                  reference={job.result_reference}
                  className="inline-flex items-center gap-1.5 text-sky-400 hover:text-sky-300 hover:underline break-all text-left disabled:opacity-70"
                >
                  <span className="break-all">{job.result_reference}</span>
                  <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                </ResolvedReferenceLink>
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

  const spawnGate = deriveSpawnGateState(task.context);

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
      {spawnGate.kind !== 'no-gate' && (
        <DetailRow label="Spawn Gate">
          {spawnGate.kind === 'unchallenged' && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-400">unchallenged</span>
          )}
          {spawnGate.kind === 'challenged-pass' && (
            <span className="inline-flex flex-col gap-1 min-w-0">
              <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-400 self-start">challenged — pass</span>
              {spawnGate.reason && <span className="text-xs text-neutral-500 break-words">{spawnGate.reason}</span>}
            </span>
          )}
          {spawnGate.kind === 'fail-open-pass' && (
            <span className="inline-flex flex-col gap-1 min-w-0">
              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-400 self-start">fail-open pass</span>
              {spawnGate.reason && <span className="text-xs text-neutral-500 break-words">{spawnGate.reason}</span>}
            </span>
          )}
        </DetailRow>
      )}
    </>
  );
}

function BlockedNeedSection({ need, tasks, onNavigateToTask }: { need: Record<string, unknown> | undefined; tasks?: Task[]; onNavigateToTask?: (id: string) => void }) {
  if (!need) return null;
  const kind = need.kind as string | undefined;
  const ref = need.ref as string | undefined;
  const label = need.label as string | undefined;
  if (!kind || !ref) return null;

  const linkedTask = kind === 'task' ? tasks?.find(t => t.id === ref) : undefined;

  return (
    <>
      <DetailRow label={`Waiting for ${kind}`}>
        <span className="inline-flex flex-col gap-1.5 min-w-0">
          {linkedTask && kind === 'task' ? (
            <button
              onClick={() => onNavigateToTask?.(ref)}
              className="text-blue-400 hover:text-blue-300 transition-colors text-left font-mono text-sm"
            >
              {linkedTask.title.length > 50 ? linkedTask.title.slice(0, 50) + '…' : linkedTask.title}
            </button>
          ) : (
            <span className="font-mono text-sm text-neutral-300 break-all">{ref}</span>
          )}
          {label && <span className="text-xs text-neutral-400">{label}</span>}
        </span>
      </DetailRow>
    </>
  );
}

/** Surface the knowledge document a completed design / web_research task
 *  produced. The completion handler mirrors the document's id, title, summary,
 *  tags and forward-quality verdict into plan_output.knowledge_document, so the
 *  drawer can link forward to the produced document without a second lookup —
 *  and fetch its full markdown body inline on demand. */
function KnowledgeDocumentSection({ task }: { task: Task }) {
  const doc = (task.plan_output as Record<string, unknown> | undefined)?.knowledge_document as {
    id?: string;
    title?: string;
    summary?: string;
    citation_count?: number;
    source_kind?: string;
    analysis_mode?: string;
    forward_quality_verdict?: { passed?: boolean; reason?: string } | null;
    compute_acceptance_grade?: string | null;
    compute_acceptance_reason?: string;
  } | undefined;
  const [expanded, setExpanded] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const projectId = task.project_id;
  const docId = doc?.id;

  if (!doc?.title) return null;
  const verdict = doc.forward_quality_verdict;

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && body === null && projectId && docId) {
      setLoading(true);
      fetchKnowledgeDocument(projectId, docId)
        .then(full => setBody(full.body ?? ''))
        .catch(() => setBody(''))
        .finally(() => setLoading(false));
    }
  };

  return (
    <>
      <SectionHeader>Knowledge Document</SectionHeader>
      <div className="space-y-2 bg-violet-950/10 rounded-lg p-3 border border-violet-900/30">
        <div className="flex items-start gap-2">
          <BookOpen className="w-4 h-4 text-violet-400 shrink-0 mt-0.5" />
          <span className="text-sm font-medium text-neutral-100 min-w-0 break-words">{doc.title}</span>
        </div>
        {doc.summary && <p className="text-xs text-neutral-400 leading-relaxed break-words">{doc.summary}</p>}
        <div className="flex items-center gap-2 flex-wrap">
          {verdict && (
            <span className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border ${verdict.passed ? 'border-emerald-800/40 bg-emerald-950/30 text-emerald-300' : 'border-red-800/40 bg-red-950/30 text-red-300'}`}>
              {verdict.passed ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
              Forward quality: {verdict.passed ? 'passed' : 'failed'}
            </span>
          )}
          {doc.analysis_mode && (
            <span className="text-[11px] px-2 py-0.5 rounded border border-neutral-700/40 bg-neutral-800/40 text-neutral-300">
              {doc.analysis_mode}
            </span>
          )}
          {doc.source_kind === 'compute_experiment' && (doc.compute_acceptance_grade === 'acceptance' || doc.compute_acceptance_grade === 'measured_only') && (() => {
            const isAcceptance = doc.compute_acceptance_grade === 'acceptance';
            const ACCEPTANCE_REASON_LABELS: Record<string, string> = {
              llm_artifact_cited: 'LLM-grounded, artifact-cited analysis — qualifies as acceptance evidence',
              analysis_mode_not_llm: 'Deterministic fallback summary (analysis mode is not LLM) — measured only',
              citation_grounding_miss: 'Citation grounding miss — measured only',
              no_source_citations: 'No source citations — measured only',
              missing_grounding_probe: 'No grounding probe recorded — measured only',
              no_grounded_artifact_citation: 'No citation resolved to a durable artifact — measured only',
            };
            const reason = typeof doc.compute_acceptance_reason === 'string' ? doc.compute_acceptance_reason : undefined;
            const tooltip = (reason ? ACCEPTANCE_REASON_LABELS[reason] : undefined)
              ?? (isAcceptance ? 'Acceptance-grade compute knowledge' : 'Measured-only compute knowledge');
            const cls = isAcceptance
              ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300'
              : 'bg-amber-500/10 border-amber-500/20 text-amber-300';
            return (
              <span
                className={`${cls} border text-[9px] uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
                title={tooltip}
              >
                {isAcceptance ? <CheckCircle className="w-2.5 h-2.5 shrink-0" /> : <HelpCircle className="w-2.5 h-2.5 shrink-0" />}
                {isAcceptance ? 'Acceptance' : 'Measured only'}
              </span>
            );
          })()}
          {doc.source_kind && (
            <span className="text-[11px] text-neutral-500">{doc.source_kind.replace(/_/g, ' ')}</span>
          )}
          {typeof doc.citation_count === 'number' && (
            <span className="text-[11px] text-neutral-500">{doc.citation_count} citation{doc.citation_count !== 1 ? 's' : ''}</span>
          )}
        </div>
        {docId && projectId && (
          <button
            onClick={toggle}
            className="inline-flex items-center gap-1.5 text-sm text-violet-300 hover:text-violet-200 transition-colors min-h-[44px]"
          >
            {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            {expanded ? 'Hide document' : 'View document'}
          </button>
        )}
        {expanded && (
          loading
            ? <p className="text-xs text-neutral-600">Loading document…</p>
            : <div className="border-t border-violet-900/30 pt-2 text-sm text-neutral-200"><MarkdownContent content={body ?? ''} /></div>
        )}
      </div>
    </>
  );
}

function EvaluationSummarySection({ summary }: { summary: TaskEvaluationSummary }) {
  const evidence = summary.evidence ?? [];
  const highlights = summary.highlights ?? [];

  const { label: verdictLabel, tone: verdictToneId } = formatVerdict(summary.verdict);
  const badgeClass = verdictToneBadgeClass(verdictToneId);

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
            {verdictLabel}
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

const PANEL_REVIEW_LENS_LABELS: Record<keyof PanelReviewVerdict['lenses'], string> = {
  correctness: 'Correctness',
  security: 'Security',
  code_quality: 'Code Quality',
  test_coverage: 'Test Coverage',
};

const PANEL_REVIEW_BADGES: Record<'APPROVE' | 'REQUEST_CHANGES', string> = {
  APPROVE: 'bg-emerald-900/40 text-emerald-300 border-emerald-800/40',
  REQUEST_CHANGES: 'bg-red-900/40 text-red-300 border-red-800/40',
};

function flattenPanelReviewIssues(verdict: PanelReviewVerdict): PanelReviewIssue[] {
  if (Array.isArray(verdict.issues) && verdict.issues.length > 0) {
    return verdict.issues;
  }
  return Object.entries(verdict.lenses).flatMap(([lensId, lens]) => (
    (lens.issues ?? []).map(issue => ({
      ...issue,
      lens: issue.lens ?? lensId as PanelReviewIssue['lens'],
    }))
  ));
}

function formatIssueLocation(issue: PanelReviewIssue): string {
  if (!issue.file) return '';
  return issue.line ? `${issue.file}:${issue.line}` : issue.file;
}

function PanelReviewVerdictSection({ verdict }: { verdict: PanelReviewVerdict }) {
  const issues = flattenPanelReviewIssues(verdict);

  return (
    <>
      <SectionHeader>Panel Review Verdict</SectionHeader>
      <div className="space-y-4 rounded-lg border border-fuchsia-900/30 bg-fuchsia-950/10 p-3">
        <div className="flex flex-wrap items-start gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-500">Overall</span>
            <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${PANEL_REVIEW_BADGES[verdict.overall_verdict]}`}>
              {verdict.overall_verdict === 'APPROVE' ? 'Approve' : 'Request Changes'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-500">Findings</span>
            <span className="text-sm text-neutral-200">{issues.length}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-500">Files Investigated</span>
            <span className="text-sm text-neutral-200">{verdict.investigated_files?.length ?? 0}</span>
          </div>
        </div>

        <div className="flex items-start gap-2">
          <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Summary</span>
          <p className="text-sm text-neutral-200 min-w-0 break-words">{verdict.summary}</p>
        </div>

        {verdict.investigated_files && verdict.investigated_files.length > 0 && (
          <div className="flex items-start gap-2">
            <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Investigated</span>
            <div className="flex min-w-0 flex-wrap gap-1.5">
              {verdict.investigated_files.map((file, index) => (
                <span key={`${file}-${index}`} className="rounded border border-fuchsia-800/40 bg-neutral-900/70 px-2 py-1 font-mono text-[11px] text-neutral-200 break-all">
                  {file}
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {Object.entries(verdict.lenses).map(([lensId, lens]) => (
            <div key={lensId} className="rounded-lg border border-neutral-800 bg-neutral-950/60 p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-neutral-100">
                  {PANEL_REVIEW_LENS_LABELS[lensId as keyof PanelReviewVerdict['lenses']]}
                </span>
                <span className={`text-[11px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${PANEL_REVIEW_BADGES[lens.verdict]}`}>
                  {lens.verdict === 'APPROVE' ? 'Approve' : 'Request Changes'}
                </span>
              </div>
              <p className="text-sm text-neutral-300 break-words">{lens.summary}</p>
              {lens.issues && lens.issues.length > 0 && (
                <div className="mt-3 space-y-2">
                  {lens.issues.map((issue, index) => (
                    <div key={`${lensId}-${index}`} className="rounded border border-neutral-800 bg-neutral-900/70 p-2">
                      <div className="flex flex-wrap items-center gap-2">
                        {issue.severity && (
                          <span className="rounded border border-neutral-700 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-300">
                            {issue.severity}
                          </span>
                        )}
                        {formatIssueLocation(issue) && (
                          <span className="inline-flex items-center gap-1 font-mono text-[11px] text-blue-300 break-all">
                            <Hash className="h-3 w-3 shrink-0" />
                            {formatIssueLocation(issue)}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-neutral-200 break-words">{issue.description}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
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

  const { prArtifacts, commitArtifacts, investigationArtifacts, storageArtifacts, mergeTestVerdict, mergeTestResult, unverifiedMergeReason } = useMemo(() => {
    const artifacts = task.artifacts ?? [];
    // Surface the no-CI merge gate's verdict. The verify_tests handler posts a
    // MERGE_TEST_RESULT artifact whose `logs` carry the JSON verdict
    // ({detected, passed, tests, output}); the orchestrator consults it to
    // decide the merge. Parse the latest such artifact (highest iteration) and
    // map it to the same pass/fail/undetected outcome the backend gate uses
    // (classify_merge_test_verdict in ci_resolution.py): detected=false →
    // undetected; passed truthy → pass; otherwise → fail.
    const mergeTestArtifacts = artifacts.filter(a => a.artifact_type === 'MERGE_TEST_RESULT' && a.logs);
    // Parse the latest MERGE_TEST_RESULT artifact and derive the top-line gate
    // verdict via the shared helper, which mirrors the backend gate's actual
    // merge decision (classify_merge_test_verdict in ci_resolution.py). This
    // surfaces documented-command / partial-setup / acceptance-probe blocks the
    // detected+passed-only derivation could not — and never reads as a clean
    // "Passed" / "No suite detected" when the gate actually blocked.
    let mergeTestVerdict: MergeTestVerdict | null = null;
    let mergeTestResult: MergeTestResult | null = null;
    if (mergeTestArtifacts.length > 0) {
      const latest = mergeTestArtifacts.reduce((a, b) => ((b.iteration ?? 1) >= (a.iteration ?? 1) ? b : a));
      try {
        mergeTestVerdict = parseMergeTestVerdict(JSON.parse(latest.logs as string));
        mergeTestResult = mergeTestVerdict ? deriveMergeTestOutcome(mergeTestVerdict) : null;
      } catch {
        mergeTestVerdict = null;
        mergeTestResult = null;
      }
    }
    return {
      prArtifacts: artifacts.filter(a => a.artifact_type === 'PULL_REQUEST' && a.pr_url && isSafeUrl(a.pr_url)),
      commitArtifacts: artifacts.filter(a => a.artifact_type === 'GIT_COMMIT'),
      investigationArtifacts: artifacts.filter(a => a.artifact_type === 'INVESTIGATION_REPORT'),
      storageArtifacts: artifacts.filter(
        (a) => !!a.storage_uri && a.artifact_type !== 'PULL_REQUEST' && a.artifact_type !== 'GIT_COMMIT',
      ),
      mergeTestVerdict,
      mergeTestResult,
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
      <DetailRow label="Last tool use">
        <span className="text-sm text-neutral-200" title={task.last_tool_use_at ? formatAbsolute(task.last_tool_use_at) : undefined}>
          {task.last_tool_use_at ? timeAgo(task.last_tool_use_at) : 'No tool use recorded'}
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
          {(() => {
            const blocker = task.context?.blocker as Record<string, unknown> | undefined;
            const need = blocker?.need as Record<string, unknown> | undefined;
            return <BlockedNeedSection need={need} tasks={tasks} onNavigateToTask={onNavigateToTask} />;
          })()}
        </>
      )}

      {(task.status === 'PARKED' || task.parked_at || task.park_reason || task.wake_registration) && (
        <>
          <SectionHeader>Parked</SectionHeader>
          {task.park_reason && (
            <DetailRow label="Reason"><span className="text-sm text-sky-200/90 break-words">{task.park_reason}</span></DetailRow>
          )}
          {task.parked_at && <DetailRow label="Parked at">{formatAbsolute(task.parked_at)}</DetailRow>}
          {task.wake_registration && (
            <>
              <DetailRow label="Wake source">
                <span className="inline-flex flex-wrap items-center gap-1.5 min-w-0">
                  <span className="font-mono text-xs text-sky-200 break-all">{task.wake_registration.wake_source_kind}</span>
                  {task.wake_registration.wake_source_ref ? (
                    <>
                      <span className="text-neutral-500">/</span>
                      <span className="font-mono text-xs text-neutral-200 break-all">{task.wake_registration.wake_source_ref}</span>
                    </>
                  ) : (
                    <span className="text-xs text-neutral-500">no source ref</span>
                  )}
                </span>
              </DetailRow>
              <DetailRow label="Fired">
                {task.wake_registration.fired_at ? (
                  formatAbsolute(task.wake_registration.fired_at)
                ) : (
                  <span className="text-sm text-neutral-500">Not yet fired</span>
                )}
              </DetailRow>
              <DetailRow label="Delivery">
                <span className="text-xs uppercase tracking-wider font-mono bg-sky-500/10 text-sky-300 border border-sky-500/20 px-2 py-0.5 rounded">
                  {formatWakeDeliveryState(task.wake_registration.delivery_state)}
                </span>
              </DetailRow>
              <DetailRow label="Deadline">
                {task.wake_registration.deadline_at ? (
                  formatAbsolute(task.wake_registration.deadline_at)
                ) : (
                  <span className="text-sm text-neutral-500">No deadline</span>
                )}
              </DetailRow>
              {(task.wake_registration.result_state || task.wake_registration.result_detail) && (
                <DetailRow label="Result">
                  <span className="text-sm text-neutral-200 break-words">
                    {task.wake_registration.result_state || 'No result state'}
                    {task.wake_registration.result_detail ? `: ${task.wake_registration.result_detail}` : ''}
                  </span>
                </DetailRow>
              )}
            </>
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

      <KnowledgeDocumentSection task={task} />

      {task.evaluation_summary && <EvaluationSummarySection summary={task.evaluation_summary} />}

      {task.task_type === 'panel_review' && task.plan_output ? (() => {
        const verdict = task.plan_output as unknown as PanelReviewVerdict;
        if (!verdict?.lenses || !verdict?.overall_verdict || !verdict?.summary) return null;
        return <PanelReviewVerdictSection verdict={verdict} />;
      })() : null}

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

      {task.plan_output && task.task_type !== 'memo' && task.task_type !== 'panel_review' && !(task.plan_output as Record<string, unknown>).knowledge_document && <>
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

      {(mergeTestResult || unverifiedMergeReason) && (() => {
        const config: Record<'pass' | 'fail' | 'blocked' | 'undetected' | 'unverified', { label: string; badge: string; note: string }> = {
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
          blocked: {
            label: 'Blocked',
            badge: 'text-red-400 bg-red-950/30 border-red-800/40',
            // Reason-specific note set below; this is the generic fallback.
            note: 'The merge gate blocked the merge on a verification failure.',
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
        const outcome: 'pass' | 'fail' | 'blocked' | 'undetected' | 'unverified' =
          unverifiedMergeReason || !mergeTestResult ? 'unverified' : mergeTestResult.outcome;
        const v = config[outcome];
        const reasonLabel = unverifiedMergeReason
          ? humanizeUnverifiedReason(unverifiedMergeReason)
          : null;
        // Reason-specific note for a gate BLOCK (documented-command /
        // partial-setup / probe failure), so the operator sees WHY the gate
        // blocked rather than the generic suite-failed wording.
        const blockedNote = (() => {
          if (outcome !== 'blocked' || !mergeTestResult) return null;
          switch (mergeTestResult.reason) {
            case 'documented_command_failed':
              return 'A command this PR introduced or documented was executed and failed — the gate blocked the merge with higher precedence than the unit suite.';
            case 'partial_setup_failed':
              return 'One or more test runners could not run their suite (environment/setup error) — the suite is only partially verified, so the gate blocked the merge.';
            case 'declared_build_failed':
              return "The project's own declared build command failed — the gate blocked the merge.";
            case 'probe_failed':
              return 'The acceptance probe failed: the changed code does not compile/build — the gate blocked the merge.';
            case 'self_suppressed_suite':
              return 'This PR disabled its own tests and thereby self-suppressed the suite — the run no longer verifies anything (all/near-all tests skipped), so the gate blocked the merge rather than accept a suite made green by skipping.';
            default:
              return v.note;
          }
        })();
        const note = reasonLabel
          ? `The merge bypassed test verification — ${reasonLabel}.`
          : blockedNote ?? v.note;
        // Documented / PR-introduced command probe — runs regardless of suite
        // detection. Surface its honest outcome (label + pass / fail /
        // could-not-run) whenever it ran.
        const documented = mergeTestVerdict?.documentedCommands?.ran ? mergeTestVerdict.documentedCommands : null;
        const documentedState = documented
          ? (documented.passed ? 'passed' : documented.could_not_run ? 'could not run' : 'failed')
          : null;
        const documentedStateClass = documented
          ? (documented.passed ? 'text-emerald-400' : documented.could_not_run ? 'text-neutral-400' : 'text-red-400')
          : '';
        const documentedLabel = (documented?.label ?? '').trim();
        const documentedFailed = documented ? documented.passed === false && !documented.could_not_run : false;
        const documentedOutput = (documented?.output ?? '').trim();
        // The acceptance probe runs only on a no-suite fall-open. Render its
        // honest outcome (ecosystem + passed / could-not-run / failed) next to
        // the suite verdict, mirroring the Evaluation Summary section.
        const probe = mergeTestVerdict?.probe?.ran ? mergeTestVerdict.probe : null;
        const probeState = probe
          ? (probe.passed ? 'passed' : probe.could_not_run ? 'could not run' : 'failed')
          : null;
        const probeStateClass = probe
          ? (probe.passed ? 'text-emerald-400' : probe.could_not_run ? 'text-neutral-400' : 'text-red-400')
          : '';
        const probeEcosystem = (probe?.ecosystem ?? '').trim();
        // declared=true means it ran the operator's OWN declared build command
        // (vs a generic ecosystem compile check); attribute it explicitly.
        const probeKind = probe?.declared ? 'declared build' : 'generic check';
        // A PASSED declared build that wrote no output is a possible no-op build.
        const probeNoArtifact = Boolean(probe?.declared && probe.passed && probe.wrote_no_artifact);
        // Best-effort coverage summary: how many of a detected suite's tests
        // actually executed vs. were skipped. Surfaced for visibility so a green
        // PASSED that exercised almost nothing is not mistaken for full coverage.
        const coverage = mergeTestVerdict?.coverage ?? null;
        const coverageMostlySkipped = mergeTestResult?.reason === 'suite_passed_mostly_skipped';
        // VISIBILITY-ONLY: the PR's own diff newly disabled tests. Surface it as
        // a prominent signal so a green gate that was made green by SKIPPING is
        // not mistaken for real coverage. Never blocks — present only when the
        // agent found at least one newly-added skip marker.
        const disabledTests = mergeTestVerdict?.disabledTests ?? null;
        const disabledMarkers = (disabledTests?.markers ?? []).slice(0, 8);
        // When the PR's own disabling self-suppressed the suite, the gate BLOCKS
        // — frame the disabledTests section as the CAUSE of the block instead of
        // the visibility-only quarantine wording.
        const disabledTestsBlocked = mergeTestResult?.reason === 'self_suppressed_suite';
        // Stale-command self-heal: the stored test command was stale and
        // auto-detection replaced it with a real suite that actually ran.
        const recovered = mergeTestVerdict?.recovered ? mergeTestVerdict : null;
        const recoveredDirs = recovered?.recovered_directories ?? [];
        // Choose the most relevant output to surface in the scrollable block:
        // the failing documented-command output (already shown above) is not
        // repeated; a probe/partial-setup BLOCK shows the block detail;
        // otherwise the captured suite output.
        const suiteOutput = (mergeTestVerdict?.output ?? '').trim();
        const testOutput = documentedFailed && documentedOutput
          ? ''
          : outcome === 'blocked' && mergeTestResult
            ? mergeTestResult.detail.trim()
            : suiteOutput;
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
              {(() => {
                const fallOpenReason = outcome === 'undetected' && mergeTestVerdict?.noTestsReason
                  ? labelNoTestsReason(mergeTestVerdict.noTestsReason)
                  : null;
                return fallOpenReason ? (
                  <div className="flex items-start gap-2">
                    <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Fall-open reason</span>
                    <span className="text-xs text-neutral-300 break-words">{fallOpenReason}</span>
                  </div>
                ) : null;
              })()}
              {documentedState && (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Documented command</span>
                  <span className={`text-xs font-semibold ${documentedStateClass}`}>
                    {documentedLabel ? `${documentedLabel}: ` : ''}{documentedState}
                  </span>
                </div>
              )}
              {probeState && (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Acceptance probe</span>
                  <span className={`text-xs font-semibold ${probeStateClass}`}>
                    {probeEcosystem ? `${probeEcosystem}: ` : ''}{probeState}
                  </span>
                  <span className="text-xs text-neutral-500">({probeKind})</span>
                </div>
              )}
              {probeNoArtifact && (
                <p className="text-xs text-amber-400 break-words">
                  ⚠ The declared build exited cleanly but wrote no output — possible no-op build (not real verification).
                </p>
              )}
              {coverage && (() => {
                const skipPct = coverage.total > 0 ? Math.round((coverage.skipped / coverage.total) * 100) : 0;
                return (
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs text-neutral-500 w-28 shrink-0">Coverage</span>
                    <span className={`text-xs font-semibold ${coverageMostlySkipped ? 'text-amber-400' : 'text-neutral-300'}`}>
                      {coverage.passed} passed · {coverage.skipped} skipped ({skipPct}%) · {coverage.total} total
                    </span>
                  </div>
                );
              })()}
              {coverageMostlySkipped && (
                <p className="text-xs text-amber-400 break-words">
                  ⚠ The suite passed but most of its tests were skipped — only a small fraction actually executed (not full verification).
                </p>
              )}
              {disabledTests && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Disabled tests</span>
                  <div className="text-xs min-w-0 flex-1">
                    <p className={`font-semibold break-words ${disabledTestsBlocked ? 'text-red-400' : 'text-amber-400'}`}>
                      {disabledTestsBlocked
                        ? `⚠ This PR disables ${disabledTests.count} test${disabledTests.count === 1 ? '' : 's'} — newly added skip/ignore markers that self-suppressed the suite; this is the cause of the merge block.`
                        : `⚠ This PR disables ${disabledTests.count} test${disabledTests.count === 1 ? '' : 's'} — newly added skip/ignore markers (visibility only; does not block the merge).`}
                    </p>
                    {disabledMarkers.length > 0 && (
                      <ul className="mt-1 space-y-0.5">
                        {disabledMarkers.map((m, i) => (
                          <li key={i} className="break-words">
                            <span className="font-mono text-blue-300">{m.file}</span>
                            {m.marker ? <span className="font-mono text-amber-300"> — {m.marker}</span> : null}
                          </li>
                        ))}
                        {disabledTests.count > disabledMarkers.length && (
                          <li className="text-neutral-500">
                            …and {disabledTests.count - disabledMarkers.length} more
                          </li>
                        )}
                      </ul>
                    )}
                  </div>
                </div>
              )}
              {recovered && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Recovered suite</span>
                  <div className="text-xs text-neutral-300 min-w-0 flex-1">
                    <p className="break-words">The stored test command was stale; auto-detection ran a replacement suite.</p>
                    {recoveredDirs.length > 0 && (
                      <ul className="mt-1 space-y-0.5">
                        {recoveredDirs.map((d, i) => (
                          <li key={i} className="break-words">
                            <span className="font-mono text-blue-300">{d.path || '.'}</span>
                            {d.type ? <span className="text-neutral-500"> ({d.type})</span> : null}
                            {d.test_cmd ? <span className="font-mono text-neutral-400"> — {d.test_cmd}</span> : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              )}
              <p className="text-xs text-neutral-400 break-words">{note}</p>
              {documentedFailed && documentedOutput && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Command Output</span>
                  <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono min-w-0 flex-1">{documentedOutput}</pre>
                </div>
              )}
              {testOutput && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Test Output</span>
                  <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono min-w-0 flex-1">{testOutput}</pre>
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
            <TeardownForensicsPanel task={task} />
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
          {storageArtifacts.length > 0 && (
            <>
              <SectionHeader>Artifacts</SectionHeader>
              <div className="space-y-2">
                {storageArtifacts.map((artifact, index) => (
                  <div
                    key={`${artifact.artifact_type}-${artifact.iteration ?? 1}-${artifact.storage_uri ?? index}`}
                    className="rounded-lg border border-neutral-800/80 bg-neutral-900/40 px-3 py-2"
                  >
                    <div className="flex flex-wrap items-center gap-2 text-[11px] mb-1">
                      <span className="rounded border border-neutral-700 bg-neutral-900/70 px-1.5 py-0.5 font-semibold text-neutral-200">
                        {formatArtifactTypeLabel(artifact.artifact_type)}
                      </span>
                      <span className="font-mono text-neutral-500">Iteration {artifact.iteration ?? 1}</span>
                      {artifact.created_at && (
                        <span className="text-neutral-500">{formatAbsolute(artifact.created_at)}</span>
                      )}
                    </div>
                    {artifact.storage_uri && (
                      <ResolvedReferenceLink
                        projectId={task.project_id}
                        reference={artifact.storage_uri}
                        className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors break-all min-w-0 min-h-[44px] text-left disabled:opacity-70"
                      >
                        <span className="break-all min-w-0">{artifact.storage_uri}</span>
                        <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                      </ResolvedReferenceLink>
                    )}
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

      {(() => {
        const ctx = task.context as Record<string, unknown> | undefined;
        const fc = ctx?._foundation_converged as { foundation_id?: string; reason?: string; shared_files?: string[] } | undefined;
        if (!fc?.foundation_id) return null;
        const foundationId = fc.foundation_id;
        const foundationTask = tasks?.find(t => t.id === foundationId);
        return (
          <>
            <SectionHeader>Structural Serialization</SectionHeader>
            <div className="space-y-2 bg-blue-950/10 rounded-lg p-3 border border-blue-900/30">
              <div className="text-sm text-blue-200">Serialized behind duplicate foundation</div>
              <DetailRow label="Foundation">
                {foundationTask && onNavigateToTask ? (
                  <button
                    onClick={() => onNavigateToTask(foundationId)}
                    className="text-blue-400 hover:text-blue-300 transition-colors text-left"
                  >
                    {foundationTask.title.length > 50 ? foundationTask.title.slice(0, 50) + '…' : foundationTask.title}
                  </button>
                ) : (
                  <span className="font-mono text-sm text-neutral-300">{foundationId}</span>
                )}
              </DetailRow>
              {fc.reason && <DetailRow label="Reason"><span className="text-sm text-blue-100/90 break-words">{fc.reason}</span></DetailRow>}
              {fc.shared_files && fc.shared_files.length > 0 && (
                <DetailRow label="Shared Files">
                  <div className="space-y-0.5">
                    {fc.shared_files.slice(0, 3).map((f, i) => <div key={i} className="text-xs text-neutral-400 font-mono break-all">{f}</div>)}
                    {fc.shared_files.length > 3 && <div className="text-xs text-neutral-500">+{fc.shared_files.length - 3} more</div>}
                  </div>
                </DetailRow>
              )}
            </div>
          </>
        );
      })()}

      {(() => {
        const ctx = task.context as Record<string, unknown> | undefined;
        const recutAttempts = ctx?._recut_recheck_attempts as number | undefined;
        const recutOutcome = ctx?._recut_last_outcome as string | undefined;
        const recutEscalated = ctx?._recut_escalated as boolean | undefined;
        if (!recutAttempts || recutOutcome === undefined) return null;
        const payload = task.payloads?.[0] as Record<string, unknown> | undefined;
        const baseBranch = (payload?.base_branch as string) || '(default)';
        const isConflict = recutOutcome === 'conflict';
        return (
          <>
            <SectionHeader>Base Re-cut Hold</SectionHeader>
            <div className={`space-y-2 rounded-lg p-3 border ${isConflict ? 'bg-red-950/10 border-red-900/30' : 'bg-orange-950/10 border-orange-900/30'}`}>
              <div className={`text-sm ${isConflict ? 'text-red-200' : 'text-orange-200'}`}>
                Held pending re-cut of base <span className="font-mono">{baseBranch}</span> from integration
              </div>
              <DetailRow label="Status">
                <span className={`inline-block px-2 py-1 rounded text-xs font-mono ${isConflict ? 'bg-red-500/20 text-red-300' : 'bg-orange-500/20 text-orange-300'}`}>
                  {isConflict ? 'Conflict' : 'Transient Error'}
                </span>
              </DetailRow>
              <DetailRow label="Attempts">{recutAttempts}</DetailRow>
              {recutEscalated && <DetailRow label="Escalation"><span className="text-xs text-red-300 font-mono">Escalated</span></DetailRow>}
            </div>
          </>
        );
      })()}

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
