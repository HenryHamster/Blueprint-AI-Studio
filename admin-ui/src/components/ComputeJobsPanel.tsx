import React, { useCallback, useEffect, useState } from 'react';
import { Cpu, Square, AlertTriangle, Clock, ExternalLink, Package, FileText } from 'lucide-react';
import { analyzeComputeJob, fetchComputeJobs, fetchKnowledgeDocument, killComputeJob, type ComputeJob, type ComputeJobCommandLog, type KnowledgeDocument } from '../api';
import { usePoller } from '../hooks/usePoller';
import { ResolvedReferenceLink } from './TaskCard';
import { ReleaseEvidenceChip } from './ReleaseEvidence';
import { asLiveReleaseEvidence } from './ReleaseEvidence.utils';
import { formatComputeAnalysisCauseLabel } from '../utils/computeAnalysisLabels';
import {
  isComputeJobActive,
  computeJobTerminalLabel,
  computeJobRpcDiagnostic,
  computeJobSpent,
  fmtUsd,
  fmtCountdown,
} from './ComputeJobsPanel.utils';

const POLL_INTERVAL_MS = 10_000;

function statusBadgeClass(job: ComputeJob): string {
  if (isComputeJobActive(job)) return 'bg-sky-500/20 text-sky-200 border-sky-500/30';
  if (job.outcome === 'SUCCESS') return 'bg-emerald-500/20 text-emerald-200 border-emerald-500/30';
  return 'bg-red-500/20 text-red-200 border-red-500/30';
}

function budgetBarClass(pct: number): string {
  if (pct >= 0.9) return 'bg-red-500';
  if (pct >= 0.7) return 'bg-amber-500';
  return 'bg-sky-500';
}

interface ComputeJobsPanelProps {
  projectId: string | null;
  onNavigateToTask?: (taskId: string) => void;
  onLaunchCompute?: () => void;
}

export function ComputeJobsPanel({ projectId, onNavigateToTask, onLaunchCompute }: ComputeJobsPanelProps) {
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [killingId, setKillingId] = useState<string | null>(null);
  const [analyzingId, setAnalyzingId] = useState<string | null>(null);
  const [analysisFeedback, setAnalysisFeedback] = useState<Record<string, { kind: 'running' | 'upgraded' | 'analysis_unavailable' | 'error'; message: string }>>({});
  const [analysisDocs, setAnalysisDocs] = useState<Record<string, KnowledgeDocument>>({});
  const [nowMs, setNowMs] = useState(() => Date.now());

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!projectId) return;
      try {
        const next = await fetchComputeJobs(projectId, signal);
        setJobs(next);
        setError(null);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        setError(e instanceof Error ? e.message : 'Failed to load compute jobs');
      } finally {
        setLoaded(true);
      }
    },
    [projectId],
  );

  usePoller((signal) => load(signal), {
    intervalMs: POLL_INTERVAL_MS,
    enabled: !!projectId,
    deps: [projectId],
  });

  // Local 1s clock drives the live deadline countdowns between polls.
  const hasActive = jobs.some(isComputeJobActive);
  useEffect(() => {
    if (!hasActive) return;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [hasActive]);

  const handleKill = useCallback(
    async (job: ComputeJob) => {
      if (!window.confirm(`Kill compute job for "${job.task_title ?? job.id}"? This stops the running VM immediately.`)) {
        return;
      }
      setKillingId(job.id);
      try {
        const updated = await killComputeJob(job.id);
        setJobs((prev) => prev.map((j) => (j.id === updated.id ? { ...j, ...updated } : j)));
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to kill compute job');
      } finally {
        setKillingId(null);
      }
    },
    [],
  );

  const handleAnalyze = useCallback(
    async (job: ComputeJob) => {
      setAnalyzingId(job.id);
      setAnalysisFeedback((prev) => ({
        ...prev,
        [job.id]: { kind: 'running', message: 'Running analysis…' },
      }));
      try {
        const result = await analyzeComputeJob(job.id);
        if (result.status === 'analysis_unavailable') {
          const causeLabel = formatComputeAnalysisCauseLabel({ unavailableCause: result.reason ?? null });
          setAnalysisFeedback((prev) => ({
            ...prev,
            [job.id]: {
              kind: 'analysis_unavailable',
              message: causeLabel ? `Analysis unavailable: ${causeLabel}` : 'Analysis unavailable',
            },
          }));
        } else {
          setAnalysisFeedback((prev) => ({
            ...prev,
            [job.id]: { kind: 'upgraded', message: 'Analysis complete' },
          }));
          if (result.knowledge_document_id && projectId) {
            try {
              const doc = await fetchKnowledgeDocument(projectId, result.knowledge_document_id);
              setAnalysisDocs((prev) => ({ ...prev, [job.id]: doc }));
            } catch {
              // Non-fatal: feedback already shown, doc fetch is best-effort
            }
          }
        }
      } catch (e) {
        setAnalysisFeedback((prev) => ({
          ...prev,
          [job.id]: {
            kind: 'error',
            message: e instanceof Error ? e.message : 'Failed to analyze compute job',
          },
        }));
      } finally {
        setAnalyzingId(null);
        await load();
      }
    },
    [load, projectId],
  );

  if (!projectId) return null;
  const showEmptyState = loaded && !error && jobs.length === 0;

  return (
    <div className="mt-4 rounded-xl border border-neutral-800 bg-neutral-950/60 p-4" data-viewport-allow-truncation>
      <div className="flex items-center gap-2 mb-3 text-[11px] font-bold text-neutral-400 uppercase tracking-widest">
        <Cpu className="w-3.5 h-3.5 shrink-0" />
        Compute jobs
      </div>

      {error && (
        <p className="text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 mb-2 break-words flex items-start gap-1.5">
          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          {error}
        </p>
      )}

      {showEmptyState && (
        <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/40 px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-semibold text-neutral-200">No compute jobs</div>
              <div className="mt-0.5 text-[10px] text-neutral-500">
                Launch a structured experiment for this project.
              </div>
            </div>
            {onLaunchCompute && (
              <button
                type="button"
                onClick={onLaunchCompute}
                className="shrink-0 min-h-[36px] rounded bg-sky-500/20 px-3 text-[11px] font-semibold text-sky-200 transition-colors hover:bg-sky-500/30"
              >
                Launch compute experiment
              </button>
            )}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2">
        {jobs.map((job) => {
          const active = isComputeJobActive(job);
          const cap = job.spend_cap_usd ?? 0;
          const spent = computeJobSpent(job);
          const pct = cap > 0 ? Math.min(1, spent / cap) : 0;
          return (
            <div
              key={job.id}
              className="rounded-lg border border-neutral-800/80 bg-neutral-900/50 px-3 py-2"
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <div className="min-w-0">
                  {job.task_id && onNavigateToTask ? (
                    <button
                      onClick={() => onNavigateToTask(job.task_id as string)}
                      className="text-xs font-semibold text-sky-300 hover:text-sky-200 hover:underline text-left break-words min-w-0"
                    >
                      {job.task_title ?? job.task_id}
                    </button>
                  ) : (
                    <div className="text-xs font-semibold text-neutral-200 break-words min-w-0">
                      {job.task_title ?? job.task_id ?? job.id}
                    </div>
                  )}
                  <div className="text-[10px] text-neutral-500 font-mono mt-0.5 break-words">
                    {job.backend}
                    {job.machine_type ? ` · ${job.machine_type}` : ''}
                    {job.accelerator_type ? ` · ${job.accelerator_type} x${job.accelerator_count ?? 1}` : ''}
                  </div>
                  {(() => {
                    const evidence = asLiveReleaseEvidence(job.polling_metadata?.live_release_evidence);
                    if (!evidence) return null;
                    return (
                      <div className="mt-1">
                        <ReleaseEvidenceChip evidence={evidence} />
                      </div>
                    );
                  })()}
                </div>
                <span
                  className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${statusBadgeClass(job)}`}
                >
                  {active ? job.status : computeJobTerminalLabel(job)}
                </span>
              </div>

              <div className="mb-1.5">
                <div className="flex items-center justify-between text-[10px] text-neutral-400 font-mono mb-1">
                  <span>Budget</span>
                  <span>
                    {fmtUsd(spent)} / {fmtUsd(job.spend_cap_usd)}
                  </span>
                </div>
                <div className="h-1.5 w-full rounded-full bg-neutral-800 overflow-hidden">
                  <div
                    className={`h-full ${budgetBarClass(pct)}`}
                    style={{ width: `${Math.round(pct * 100)}%` }}
                  />
                </div>
              </div>

              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-[10px] text-neutral-400 font-mono min-w-0">
                  <Clock className="w-3 h-3 shrink-0" />
                  {active ? (
                    <span className="break-words">{fmtCountdown(job.wall_clock_deadline, nowMs)} left</span>
                  ) : (
                    <span className="break-words">{job.terminal_reason ?? '—'}</span>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {!active && (
                    <button
                      onClick={() => void handleAnalyze(job)}
                      disabled={analyzingId === job.id}
                      className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-violet-500/20 text-violet-200 hover:bg-violet-500/30 text-[11px] font-semibold px-3 transition-colors disabled:opacity-50"
                      title="Run analysis for this terminal compute job"
                    >
                      <Cpu className="w-3 h-3" />
                      {analyzingId === job.id ? 'Analyzing…' : 'Run analysis'}
                    </button>
                  )}
                  {active && (
                    <button
                      onClick={() => void handleKill(job)}
                      disabled={killingId === job.id}
                      className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-red-500/20 text-red-200 hover:bg-red-500/30 text-[11px] font-semibold px-3 transition-colors disabled:opacity-50"
                      title="Kill this compute job"
                    >
                      <Square className="w-3 h-3" />
                      {killingId === job.id ? 'Killing…' : 'Kill'}
                    </button>
                  )}
                </div>
              </div>
              {(() => {
                // Provisioning-death evidence: for a job that went terminal
                // before the VM ever reached RUNNING ($0 cost), surface the VM
                // phase, the sanitized launch-RPC cause, and the requested
                // launch contract so an operator can see WHY it died (deadline
                // too tight for GPU cold start, quota/capacity, launch error).
                // When none of the new fields are present we render nothing
                // extra and the terminal_reason above stands on its own.
                if (active || job.vm_phase !== 'provisioning') return null;
                const cause = computeJobRpcDiagnostic(job);
                const deadlineSec = job.launch_contract?.requested_deadline_seconds;
                const provModel = job.launch_contract?.provisioning_model;
                const hasContract =
                  typeof deadlineSec === 'number' || (typeof provModel === 'string' && !!provModel);
                if (!cause && !hasContract) return null;
                return (
                  <div
                    className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[10px] text-amber-200"
                    data-testid="compute-provisioning-cause"
                  >
                    <div className="flex items-start gap-1.5">
                      <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <div className="font-semibold break-words">
                          Died during provisioning — VM never reached RUNNING
                        </div>
                        {cause && (
                          <div className="mt-0.5 font-mono text-amber-300/90 break-words">
                            {cause.operation}: {cause.error_type}
                            {cause.error_message ? ` — ${cause.error_message}` : ''}
                          </div>
                        )}
                        {hasContract && (
                          <div className="mt-0.5 font-mono text-amber-300/80 break-words">
                            {typeof deadlineSec === 'number'
                              ? `requested deadline ${deadlineSec}s`
                              : ''}
                            {typeof deadlineSec === 'number' && provModel ? ' · ' : ''}
                            {provModel ? `provisioning model ${provModel}` : ''}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })()}
              {analysisFeedback[job.id] && (
                <div
                  className={`mt-1.5 rounded border px-2 py-1 text-[10px] ${
                    analysisFeedback[job.id].kind === 'error'
                      ? 'border-red-500/30 bg-red-500/10 text-red-200'
                      : analysisFeedback[job.id].kind === 'analysis_unavailable'
                        ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
                        : analysisFeedback[job.id].kind === 'upgraded'
                          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
                          : 'border-violet-500/30 bg-violet-500/10 text-violet-200'
                  }`}
                >
                  {analysisFeedback[job.id].message}
                </div>
              )}
              {(() => {
                const doc = analysisDocs[job.id];
                if (!doc) return null;
                return (
                  <div
                    className="mt-1.5 rounded border border-violet-500/30 bg-violet-500/10 px-2 py-1.5 text-[10px]"
                    data-testid="compute-analysis-doc"
                  >
                    <div className="flex items-start gap-1.5">
                      <FileText className="w-3 h-3 shrink-0 mt-0.5 text-violet-300" />
                      <div className="min-w-0">
                        <div className="font-semibold text-violet-200 break-words">{doc.title}</div>
                        {doc.summary && (
                          <div className="mt-0.5 text-violet-300/80 break-words whitespace-pre-wrap">{doc.summary}</div>
                        )}
                        {doc.body && !doc.summary && (
                          <div className="mt-0.5 text-violet-300/80 break-words line-clamp-4 whitespace-pre-wrap">
                            {doc.body.slice(0, 400)}{doc.body.length > 400 ? '…' : ''}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })()}
              {job.artifact_id && (
                <div className="mt-1.5 flex items-start gap-1.5 text-[10px]">
                  <Package className="w-3 h-3 shrink-0 mt-px text-emerald-300" />
                  <div className="min-w-0">
                    {job.result_reference ? (
                      <ResolvedReferenceLink
                        projectId={projectId}
                        reference={job.result_reference}
                        className="inline-flex items-center gap-1 font-semibold text-emerald-300 hover:text-emerald-200 hover:underline break-words min-w-0 text-left disabled:opacity-70"
                      >
                        <span className="break-words">COMPUTE_RESULT artifact</span>
                        <ExternalLink className="w-3 h-3 shrink-0" />
                      </ResolvedReferenceLink>
                    ) : (
                      <span className="font-semibold text-emerald-300">
                        COMPUTE_RESULT artifact
                      </span>
                    )}
                  </div>
                </div>
              )}
              {job.result_reference && (
                <div className="mt-1.5 flex items-start gap-1.5 text-[10px] text-neutral-400 font-mono break-words">
                  <span className="shrink-0 text-neutral-500">Result reference</span>
                  <ResolvedReferenceLink
                    projectId={projectId}
                    reference={job.result_reference}
                    className="inline-flex items-center gap-1 text-sky-300 hover:text-sky-200 hover:underline break-all min-w-0 text-left disabled:opacity-70"
                  >
                    <span className="break-all">{job.result_reference}</span>
                    <ExternalLink className="w-3 h-3 shrink-0" />
                  </ResolvedReferenceLink>
                </div>
              )}
              {(() => {
                const metrics = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
                if (!metrics) return null;
                const entries = Object.entries(metrics);
                if (entries.length === 0) return null;
                return (
                  <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-neutral-400 font-mono">
                    {entries.map(([k, v]) => (
                      <span key={k}>
                        <span className="text-neutral-500">{k}:</span> {String(v)}
                      </span>
                    ))}
                  </div>
                );
              })()}
              {(() => {
                if (active) return null;
                const sc = job.success_criterion;
                const verdict = job.criterion_verdict;
                if (!sc) return null;
                const expr = `${sc.metric} ${sc.operator} ${sc.threshold}`;
                let chip: React.ReactNode;
                if (!verdict) {
                  chip = (
                    <span className="rounded border border-neutral-600/40 bg-neutral-800/60 px-1 py-0.5 text-neutral-400">
                      not reported
                    </span>
                  );
                } else if (verdict.met) {
                  chip = (
                    <span
                      className="rounded border border-emerald-500/40 bg-emerald-500/15 px-1 py-0.5 text-emerald-300"
                      data-testid="criterion-met"
                    >
                      met
                    </span>
                  );
                } else {
                  chip = (
                    <span
                      className="rounded border border-red-500/40 bg-red-500/15 px-1 py-0.5 text-red-300"
                      data-testid="criterion-unmet"
                    >
                      {verdict.measurable ? 'unmet' : 'unmet (not measurable)'}
                    </span>
                  );
                }
                return (
                  <div
                    className="mt-1.5 flex items-center gap-1.5 text-[10px] font-mono"
                    data-testid="criterion-verdict-row"
                  >
                    <span className="text-neutral-500 shrink-0">Criterion</span>
                    <span className="text-neutral-300 break-words">{expr}</span>
                    {verdict && verdict.value !== null && verdict.value !== undefined && (
                      <span className="text-neutral-500">
                        (measured: {verdict.value})
                      </span>
                    )}
                    {chip}
                  </div>
                );
              })()}
              {(() => {
                if (active) return null;
                const cl = job.polling_metadata?.command_log as ComputeJobCommandLog | undefined | null;
                if (!cl || typeof cl.text !== 'string' || !cl.text.trim()) return null;
                const lines = cl.text.split('\n');
                const displayLines = lines.length > 30 ? lines.slice(-30) : lines;
                const clipped = lines.length > 30;
                return (
                  <div className="mt-1.5" data-testid="compute-command-log">
                    <div className="flex items-center gap-1.5 mb-0.5 text-[10px] text-neutral-500 font-mono">
                      <span>Command log</span>
                      {(cl.truncated || clipped) && (
                        <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-[9px] text-neutral-400">
                          truncated
                        </span>
                      )}
                    </div>
                    <pre className="rounded border border-neutral-800 bg-neutral-950 px-2 py-1.5 text-[9px] text-neutral-300 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
                      {displayLines.join('\n')}
                    </pre>
                  </div>
                );
              })()}
              {(() => {
                const diag = computeJobRpcDiagnostic(job);
                if (!diag) return null;
                // Provisioning-phase terminal failures get the dedicated
                // provisioning-cause block above (with the launch contract);
                // don't also render the generic retry-oriented RPC warning.
                if (!active && job.vm_phase === 'provisioning') return null;
                // Retry context: prefer a live countdown to next_retry_at, fall
                // back to the bounded retry_after_seconds the backend recorded.
                let retryText: string | null = null;
                if (diag.next_retry_at) {
                  retryText = `retry in ${fmtCountdown(diag.next_retry_at, nowMs)}`;
                } else if (typeof diag.retry_after_seconds === 'number') {
                  retryText = `retry after ${Math.max(0, Math.round(diag.retry_after_seconds))}s`;
                }
                return (
                  <div
                    className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[10px] text-amber-200"
                    data-testid="compute-rpc-diagnostic"
                  >
                    <div className="flex items-start gap-1.5">
                      <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <div className="font-semibold break-words">
                          Backend RPC issue: {diag.operation}
                          {' · '}
                          attempt {diag.attempt_count}
                          {retryText ? ` · ${retryText}` : ''}
                        </div>
                        <div className="mt-0.5 font-mono text-amber-300/90 break-words">
                          {diag.error_type}: {diag.error_message}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })()}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default ComputeJobsPanel;
