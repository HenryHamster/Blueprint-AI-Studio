import { useState, useEffect, useMemo } from 'react';
import { ChevronDown, ChevronRight, ExternalLink, Clock } from 'lucide-react';
import { getHeartbeatStatus, useElapsedTime, ResearchFindings, useDurationStats } from './TaskCard';
import { formatAbsolute, parseTimestamp, timeAgo, formatDurationSec } from '../utils/time';
import { hasTransitiveCycle } from '../utils/taskGraph';
import { authGet, API_BASE_URL } from '../api';
import { TaskFixLoopPanel } from './TaskFixLoopPanel';
import { SelfReviewPanel } from './TaskDetailDrawerSelfReview';
import { TaskDiagnosticsPanel, TaskErrorLogsPanel } from './TaskDetailDrawerDiagnostics';
import { FailureDetailPanel, TeardownForensicsPanel } from './TaskDetailDrawerFailureDetail';
import { TaskSideboardContextSection } from './TaskDetailDrawerSideboardContext';
import type { AdversarialGateDiagnostic, PanelReviewVerdict, Task } from '../types/task';
import { CopyableId, SectionHeader, DetailRow, DirectionsSection, DependencyItem } from './TaskDetailDrawerPrimitives';
import {
  TaskComputeJobsSection,
  ResearchOriginSection,
  SalvageActivitySection,
  AdversarialGateDiagnosticSection,
  KnowledgeDocumentSection,
  EvaluationSummarySection,
  PanelReviewVerdictSection,
  AcceptanceEvidenceSection,
  SystemMetricsSection,
  BlockerSection,
  ParkedSection,
  TaskConfigSection,
  MemoReasoningSection,
  InvestigationReportSection,
  MergeTestGateSection,
  StatusLogSection,
  HistorySection,
  CodeOutputSection,
  StructuralSerializationSection,
  BaseRecutHoldSection,
  ChildTasksSection,
  ViewportScreenshotsPanel,
} from './TaskDetailDrawerSections';

function formatCreatedBy(value: string): string {
  if (value === 'USER') return 'User';
  if (value === 'ORCHESTRATOR') return 'Orchestrator';
  if (value.startsWith('AGENT_')) return `Agent (${value.slice(6)})`;
  return value;
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
  const taskById = useMemo(() => new Map((tasks ?? []).map(t => [t.id, t])), [tasks]);

  const provenanceCtx = task.context as Record<string, unknown> | undefined;
  const provenanceDirectionId = provenanceCtx?.direction_id as string | undefined;
  const hasSideboardDirection = Boolean(task.sideboard_context?.strategic.direction);
  const llmProvider = typeof provenanceCtx?.llm_provider === 'string' ? provenanceCtx.llm_provider : null;
  const llmModel = typeof provenanceCtx?.llm_model === 'string' ? provenanceCtx.llm_model : null;

  useEffect(() => {
    if (!provenanceDirectionId || !task.project_id || hasSideboardDirection) return;
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
  }, [task.project_id, provenanceDirectionId, hasSideboardDirection]);

  return (
    <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-1">
      <TaskSideboardContextSection task={task} onNavigateToTask={onNavigateToTask} />
      <SectionHeader>Identifiers</SectionHeader>
      <DetailRow label="Task ID"><CopyableId value={task.id} /></DetailRow>
      {task.created_by && (
        <DetailRow label="Created By"><span className="text-sm">{formatCreatedBy(task.created_by)}</span></DetailRow>
      )}
      {task.parent_task_id && (
        <DetailRow label="Parent Task">
          <DependencyItem dep={task.parent_task_id} depTask={taskById.get(task.parent_task_id)} onNavigateToTask={onNavigateToTask} />
        </DetailRow>
      )}
      <ResearchOriginSection task={task} tasks={tasks} onNavigateToTask={onNavigateToTask} />
      <SalvageActivitySection task={task} onNavigateToTask={onNavigateToTask} />
      {task.context?.adversarial_gate_diagnostic && (
        <AdversarialGateDiagnosticSection diag={task.context.adversarial_gate_diagnostic as AdversarialGateDiagnostic} onNavigateToTask={onNavigateToTask} />
      )}
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
        const progressPct = Math.round((elapsedSec / typeStat.p50_seconds) * 100);
        const isOver = remaining <= 0;
        const label = remaining > 0 ? `~${Math.ceil(remaining / 60)}m left` : 'longer than usual';
        const statusLabel = isOver ? 'Over p50' : 'Within p50';
        const statusColor = isOver ? 'text-amber-400' : 'text-emerald-400';
        return (
          <>
            <DetailRow label="p50 Duration">
              <span className="font-mono text-sm text-neutral-200" title={`Based on ${typeStat.sample_count} recent ${taskType} tasks`}>
                {formatDurationSec(typeStat.p50_seconds)}
              </span>
            </DetailRow>
            <DetailRow label="Progress">
              <div className="flex items-center gap-2">
                <div className="flex-1 bg-neutral-700 rounded-full h-2 max-w-xs overflow-hidden">
                  <div
                    className={`h-full transition-all ${isOver ? 'bg-amber-500' : 'bg-emerald-500'}`}
                    style={{ width: `${Math.min(progressPct, 100)}%` }}
                  />
                </div>
                <span className={`font-mono text-sm whitespace-nowrap ${statusColor}`} title={`${progressPct}% of p50 duration elapsed`}>
                  {progressPct}% {statusLabel}
                </span>
              </div>
            </DetailRow>
            <DetailRow label="Est. remaining">
              <span className={`font-mono text-sm ${isOver ? 'text-amber-400' : 'text-neutral-200'}`} title={`Based on ${typeStat.sample_count} recent ${taskType} tasks`}>
                {label}
              </span>
            </DetailRow>
          </>
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
        const llmCost = task.task_llm_cost_usd;
        const llmCostApprox = task.task_llm_cost_any_unpriced;
        const vmCost = task.estimated_cost_usd;
        return (
          <>
            <DetailRow label="LLM Usage">
              <span className="font-mono text-sm">
                {calls.toLocaleString()} calls · {input.toLocaleString()} in / {output.toLocaleString()} out tokens
                {llmCost != null && (
                  <span className="text-neutral-400"> · {llmCostApprox ? '~' : ''}${llmCost.toFixed(4)}</span>
                )}
              </span>
            </DetailRow>
            {vmCost != null && (
              <DetailRow label="VM Cost">
                <span className="font-mono text-sm text-neutral-400">${vmCost.toFixed(2)}</span>
              </DetailRow>
            )}
            {(task.vm_machine_type != null || task.vm_warm_start != null) && (
              <DetailRow label="VM Type">
                <span className="font-mono text-sm flex flex-wrap gap-x-2 gap-y-0.5">
                  {task.vm_machine_type && <span className="text-neutral-300">{task.vm_machine_type}</span>}
                  {task.vm_spot && <span className="text-amber-400">spot</span>}
                  {task.vm_warm_start != null && (
                    <span className={task.vm_warm_start ? 'text-emerald-400' : 'text-sky-400'}>
                      {task.vm_warm_start ? 'warm' : 'cold'}
                      {!task.vm_warm_start && task.vm_cold_start_ms != null && (
                        <span className="text-neutral-400"> ({task.vm_cold_start_ms.toLocaleString()}ms)</span>
                      )}
                    </span>
                  )}
                </span>
              </DetailRow>
            )}
          </>
        );
      })()}
      {llmProvider && <DetailRow label="LLM Provider"><span className="font-mono text-sm break-all">{llmProvider}</span></DetailRow>}
      {llmModel && <DetailRow label="LLM Model"><span className="font-mono text-sm break-all">{llmModel}</span></DetailRow>}

      <SystemMetricsSection task={task} />

      <BlockerSection task={task} now30={now30} tasks={tasks} onNavigateToTask={onNavigateToTask} />

      {task.status === 'COMPLETED' && task.post_commit_pending && (
        <DetailRow label="Post-commit">
          <span className="inline-flex items-center gap-1 text-amber-400 text-sm">
            <Clock className="w-3.5 h-3.5 shrink-0" />
            Effects pending — PR submission, child spawns, or merge queued but not yet fired
          </span>
        </DetailRow>
      )}

      <ParkedSection task={task} />

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

      <TaskConfigSection task={task} />

      <KnowledgeDocumentSection task={task} />

      {task.evaluation_summary && <EvaluationSummarySection summary={task.evaluation_summary} />}

      {task.task_type === 'panel_review' && task.plan_output ? (() => {
        const verdict = task.plan_output as unknown as PanelReviewVerdict;
        if (!verdict?.lenses || !verdict?.overall_verdict || !verdict?.summary) return null;
        return <PanelReviewVerdictSection verdict={verdict} />;
      })() : null}

      <MemoReasoningSection task={task} onNavigateToTask={onNavigateToTask} />

      {task.plan_output && task.task_type !== 'memo' && task.task_type !== 'panel_review' && !(task.plan_output as Record<string, unknown>).knowledge_document && <>
        <SectionHeader>Plan Output</SectionHeader>
        <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-purple-950/20 rounded-lg p-3 max-h-64 overflow-y-auto custom-scrollbar border border-purple-900/30 font-mono">{JSON.stringify(task.plan_output, null, 2)}</pre>
      </>}

      {task.research_findings && <>
        <SectionHeader>Research Findings ({task.research_findings.length})</SectionHeader>
        <div className="bg-cyan-950/20 rounded-lg p-3 border border-cyan-900/30"><ResearchFindings findings={task.research_findings} /></div>
      </>}

      <InvestigationReportSection task={task} />

      <MergeTestGateSection task={task} />

      <ViewportScreenshotsPanel task={task} />

      <AcceptanceEvidenceSection task={task} />

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

      <StatusLogSection task={task} />

      <TaskFixLoopPanel
        taskId={task.id}
        fixLoopAbandoned={task.context?.fix_loop_abandoned}
      />

      <SelfReviewPanel task={task} />

      <HistorySection task={task} />

      <CodeOutputSection task={task} />

      {task.depends_on && task.depends_on.length > 0 && (
        <>
          <SectionHeader>Dependencies</SectionHeader>
          <div className="space-y-1">
            {task.depends_on.map((dep) => { const dt = taskById.get(dep); return <DependencyItem key={dep} dep={dep} depTask={dt} hasCycle={hasTransitiveCycle(task.id, dep, tasks ?? [], 10)} onNavigateToTask={onNavigateToTask} />; })}
          </div>
        </>
      )}

      <StructuralSerializationSection task={task} tasks={tasks} onNavigateToTask={onNavigateToTask} />

      <BaseRecutHoldSection task={task} />

      <ChildTasksSection task={task} tasks={tasks} onNavigateToTask={onNavigateToTask} />
    </div>
  );
}
