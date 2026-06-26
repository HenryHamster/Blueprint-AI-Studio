import React, { useCallback, useEffect, useState } from 'react';
import { Cpu, Square, AlertTriangle, Clock, ExternalLink, Package, FileText, LayoutGrid } from 'lucide-react';
import { analyzeComputeJob, fetchComputeJobsPaged, fetchComputeJobsAggregateSummary, fetchKnowledgeDocument, killComputeJob, type ComputeJob, type ComputeJobCommandLog, type ComputeJobLiveOutput, type ComputeJobsSummary, type KnowledgeDocument } from '../api';
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
  computeJobRuntimeSec,
  computeJobBurnRate,
  computeJobProjectedSpend,
  computeJobMargin,
  fmtUsd,
  fmtCountdown,
  fmtDuration,
} from './ComputeJobsPanel.utils';

const POLL_INTERVAL_MS = 10_000;

const DIAGNOSTIC_KEYS = new Set(['exit_code', 'error', 'reason', 'stderr_excerpt', 'label', 'note', 'log']);

const WINDOW_OPTIONS: { label: string; minutes: number }[] = [
  { label: '6h', minutes: 360 },
  { label: '24h', minutes: 1440 },
  { label: '3d', minutes: 4320 },
  { label: '7d', minutes: 10080 },
];

type MetricDirection = 'higher_better' | 'lower_better' | 'unknown';

function metricDirection(key: string, group: ComputeJob[]): MetricDirection {
  if (key === '__cost__' || key === '__runtime__') return 'lower_better';
  for (const job of group) {
    const v = job.criterion_verdict;
    if (v && v.metric === key) {
      return v.operator === '>=' || v.operator === '>' ? 'higher_better' : 'lower_better';
    }
  }
  return 'unknown';
}

function bestIndex(values: (number | null)[], dir: MetricDirection): number | null {
  if (dir === 'unknown') return null;
  let bi: number | null = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v === null) continue;
    if (bi === null) { bi = i; continue; }
    const bv = values[bi] ?? 0;
    if (dir === 'higher_better' ? v > bv : v < bv) bi = i;
  }
  return bi;
}

function jobMetricValue(job: ComputeJob, key: string, criterionMetric: string | null): number | null {
  if (key === '__cost__') return computeJobSpent(job);
  if (key === '__runtime__') return computeJobRuntimeSec(job);
  if (key === criterionMetric && job.criterion_verdict?.value != null) return job.criterion_verdict.value;
  const rm = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
  if (key.startsWith('arms.')) {
    const dot = key.indexOf('.', 5);
    if (dot === -1) return null;
    const arms = rm?.['arms'];
    if (!arms || typeof arms !== 'object' || Array.isArray(arms)) return null;
    const armData = (arms as Record<string, unknown>)[key.slice(5, dot)];
    if (!armData || typeof armData !== 'object' || Array.isArray(armData)) return null;
    const v = (armData as Record<string, unknown>)[key.slice(dot + 1)];
    return typeof v === 'number' ? v : null;
  }
  const v = rm?.[key];
  return typeof v === 'number' ? v : null;
}

function formatMetricLabel(key: string): string {
  if (key === '__cost__') return 'Cost';
  if (key === '__runtime__') return 'Runtime';
  if (key.startsWith('arms.')) {
    const dot = key.indexOf('.', 5);
    if (dot !== -1) return `${key.slice(5, dot)} / ${key.slice(dot + 1)}`;
  }
  return key;
}

function formatMetricValue(key: string, v: number | null): string {
  if (v === null) return '—';
  if (key === '__cost__') return fmtUsd(v);
  if (key === '__runtime__') return fmtDuration(v);
  return String(v);
}

type ArmRecord = { name: string; metrics: Record<string, unknown> };

function normalizeArms(arms: unknown): ArmRecord[] | null {
  if (!arms || typeof arms !== 'object') return null;
  if (Array.isArray(arms)) {
    const records = arms
      .filter((a): a is Record<string, unknown> => a !== null && typeof a === 'object' && !Array.isArray(a))
      .map((a) => {
        const name = String(a.name ?? a.arm ?? 'arm');
        const metrics: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(a)) {
          if (k !== 'name' && k !== 'arm') metrics[k] = v;
        }
        return { name, metrics };
      });
    return records.length > 0 ? records : null;
  }
  const records: ArmRecord[] = [];
  for (const [name, val] of Object.entries(arms as Record<string, unknown>)) {
    if (val !== null && typeof val === 'object' && !Array.isArray(val)) {
      records.push({ name, metrics: val as Record<string, unknown> });
    }
  }
  return records.length > 0 ? records : null;
}

type ArmVerdict = {
  winnerName: string;
  winnerValue: number;
  runnerUpName: string;
  runnerUpValue: number;
  margin: number;
};

function armComparisonVerdict(arms: ArmRecord[], metricKey: string, direction: MetricDirection): ArmVerdict | null {
  if (direction === 'unknown') return null;
  const measured = arms
    .map((arm) => ({ name: arm.name, value: typeof arm.metrics[metricKey] === 'number' ? arm.metrics[metricKey] as number : null }))
    .filter((a): a is { name: string; value: number } => a.value !== null);
  if (measured.length < 2) return null;
  const sorted = [...measured].sort((a, b) => direction === 'higher_better' ? b.value - a.value : a.value - b.value);
  const winner = sorted[0];
  const runnerUp = sorted[1];
  const margin = direction === 'higher_better' ? winner.value - runnerUp.value : runnerUp.value - winner.value;
  return { winnerName: winner.name, winnerValue: winner.value, runnerUpName: runnerUp.name, runnerUpValue: runnerUp.value, margin };
}

function RunComparisonMatrix({ group }: { group: ComputeJob[] }) {
  let criterionMetric: string | null = null;
  const extraKeys = new Set<string>();
  for (const job of group) {
    if (!criterionMetric && job.criterion_verdict?.metric) criterionMetric = job.criterion_verdict.metric;
    const rm = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
    if (rm) for (const k of Object.keys(rm)) if (k !== criterionMetric && typeof rm[k] === 'number') extraKeys.add(k);
  }

  const allKeys: string[] = [
    ...(criterionMetric ? [criterionMetric] : []),
    ...extraKeys,
    '__cost__',
    '__runtime__',
  ];

  const rows = allKeys
    .map((key) => {
      const dir = metricDirection(key, group);
      const values = group.map((job) => jobMetricValue(job, key, criterionMetric));
      return { key, dir, values, bi: bestIndex(values, dir) };
    })
    .filter((r) => r.values.some((v) => v !== null));

  const metCount = group.filter((j) => j.criterion_verdict?.met).length;

  return (
    <div data-testid="run-comparison-matrix">
      <div className="mb-1 text-ui-sm font-mono text-neutral-400">{metCount}/{group.length} met criterion</div>
      <div className="overflow-x-auto">
        <table className="w-full text-ui-sm font-mono">
          <thead>
            <tr>
              <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Metric</th>
              {group.map((job) => (
                <th key={job.id} className="text-right pr-2 pb-1 text-neutral-300 font-semibold max-w-28 truncate">
                  {(job.task_title ?? job.id).slice(0, 24)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ key, values, bi }) => (
              <tr key={key} className="border-t border-neutral-800/50">
                <td className="py-0.5 pr-3 text-neutral-500">{formatMetricLabel(key)}</td>
                {values.map((v, i) => (
                  <td
                    key={i}
                    className={`py-0.5 pr-2 text-right ${i === bi ? 'text-emerald-300 font-semibold' : 'text-neutral-400'}`}
                    data-testid={i === bi ? 'matrix-best-cell' : undefined}
                  >
                    {formatMetricValue(key, v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1.5" data-testid="matrix-verdict-summary">
        {group.map((job) => {
          const v = job.criterion_verdict;
          return (
            <span
              key={job.id}
              className={`rounded border px-1.5 py-0.5 text-ui-xs font-semibold ${v?.met ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : v ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-neutral-600/40 bg-neutral-800/60 text-neutral-400'}`}
              data-testid="matrix-verdict-chip"
            >
              {(job.task_title ?? job.id).slice(0, 20)}: {v?.met ? 'met' : v ? 'missed' : 'no verdict'}
            </span>
          );
        })}
      </div>
      {(() => {
        const jobsWithArms = group.filter((job) => {
          const rm = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
          return rm && normalizeArms(rm.arms) !== null;
        });
        if (jobsWithArms.length === 0) return null;
        return (
          <div className="mt-2" data-testid="arms-comparison">
            <div className="text-ui-sm text-neutral-500 font-mono mb-1">Per-arm results</div>
            {jobsWithArms.map((job) => {
              const rm = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
              const arms = normalizeArms(rm?.arms);
              if (!arms) return null;
              return (
                <div key={job.id} className="mb-1.5">
                  <div className="text-ui-sm font-semibold text-neutral-300 font-mono mb-0.5">
                    {(job.task_title ?? job.id).slice(0, 24)}
                  </div>
                  {arms.map((arm) => {
                    const armExitCode = typeof arm.metrics.exit_code === 'number' ? arm.metrics.exit_code : null;
                    const armError = typeof arm.metrics.error === 'string' ? arm.metrics.error : typeof arm.metrics.reason === 'string' ? arm.metrics.reason : null;
                    const armFailed = (armExitCode !== null && armExitCode !== 0) || armError !== null;
                    return (
                      <div key={arm.name} className={`flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm font-mono mb-0.5 pl-2`}>
                        <span className={`font-semibold ${armFailed ? 'text-red-300' : 'text-neutral-400'}`}>{arm.name}:</span>
                        {armExitCode !== null && (
                          <span
                            className={`rounded border px-1 py-0.5 text-ui-xs ${armFailed ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-neutral-700 bg-neutral-800/60 text-neutral-400'}`}
                            data-testid="arm-exit-code"
                          >
                            exit {armExitCode}
                          </span>
                        )}
                        {Object.entries(arm.metrics).filter(([mk]) => !DIAGNOSTIC_KEYS.has(mk)).map(([mk, mv]) => (
                          <span key={mk}>
                            <span className="text-neutral-500">{mk}:</span>{' '}
                            <span className="text-neutral-300">{String(mv)}</span>
                          </span>
                        ))}
                        {armError && (
                          <span className="text-red-300/90 break-words" data-testid="arm-error">{armError.slice(0, 120)}{armError.length > 120 ? '…' : ''}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        );
      })()}
    </div>
  );
}

function groupJobsByExperiment(jobs: ComputeJob[]): Map<string, ComputeJob[]> {
  const groups = new Map<string, ComputeJob[]>();
  for (const job of jobs) {
    if (isComputeJobActive(job)) continue;
    const key = job.experiment ?? job.benchmark;
    if (!key) continue;
    const group = groups.get(key) ?? [];
    group.push(job);
    groups.set(key, group);
  }
  return groups;
}

function bestRunId(group: ComputeJob[]): string | null {
  const measurable = group.filter((j) => j.criterion_verdict?.value != null);
  if (measurable.length === 0) return null;
  const op = measurable[0].criterion_verdict?.operator ?? '>=';
  const higherBetter = op === '>=' || op === '>';
  return measurable.reduce((best, job) => {
    const bv = best.criterion_verdict?.value ?? 0;
    const jv = job.criterion_verdict?.value ?? 0;
    return higherBetter ? (jv > bv ? job : best) : (jv < bv ? job : best);
  }, measurable[0]).id;
}

function ExperimentSweepSummary({ jobs }: { jobs: ComputeJob[] }) {
  const groups = groupJobsByExperiment(jobs);
  const multiRun = [...groups.entries()].filter(([, g]) => g.length >= 2);
  const [compareSet, setCompareSet] = useState<Set<string>>(new Set());
  if (multiRun.length === 0) return null;

  function toggleCompare(expName: string) {
    setCompareSet((prev) => {
      const next = new Set(prev);
      if (next.has(expName)) next.delete(expName); else next.add(expName);
      return next;
    });
  }

  return (
    <div className="mb-3 flex flex-col gap-2">
      {multiRun.map(([expName, group]) => {
        const metCount = group.filter((j) => j.criterion_verdict?.met).length;
        const best = bestRunId(group);
        const comparing = compareSet.has(expName);
        return (
          <div
            key={expName}
            className="rounded-lg border border-violet-500/20 bg-violet-500/5 px-3 py-2"
            data-testid="experiment-sweep-summary"
          >
            <div className="flex items-center justify-between gap-2 mb-1.5">
              <span className="text-ui-sm font-semibold text-violet-300 break-words">{expName}</span>
              <div className="flex items-center gap-2 shrink-0">
                {!comparing && (
                  <span className="text-ui-sm text-neutral-400 font-mono" data-testid="sweep-criterion-count">
                    {metCount}/{group.length} met criterion
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => toggleCompare(expName)}
                  className="min-h-[44px] flex items-center gap-1 rounded px-1.5 py-0.5 text-ui-xs font-semibold text-violet-400 hover:text-violet-200 hover:bg-violet-500/15 transition-colors"
                  data-testid="sweep-compare-toggle"
                >
                  <LayoutGrid className="w-3 h-3" />
                  {comparing ? 'List' : 'Compare'}
                </button>
              </div>
            </div>
            {comparing ? (
              <RunComparisonMatrix group={group} />
            ) : (
              <div className="flex flex-col gap-0.5">
                {group.map((job) => {
                  const isBest = job.id === best;
                  const verdict = job.criterion_verdict;
                  return (
                    <div
                      key={job.id}
                      className={`flex items-center gap-2 text-ui-sm font-mono ${isBest ? 'text-emerald-300' : 'text-neutral-400'}`}
                      data-testid={isBest ? 'sweep-best-run' : 'sweep-run-row'}
                    >
                      <span className="min-w-0 flex-1 truncate">{job.task_title ?? job.id}</span>
                      {verdict?.value != null && (
                        <span className={isBest ? 'font-semibold' : ''}>{verdict.value}</span>
                      )}
                      {verdict ? (
                        verdict.met ? (
                          <span className="shrink-0 rounded border border-emerald-500/40 bg-emerald-500/15 px-1 py-0.5 text-ui-xs text-emerald-300">
                            met
                          </span>
                        ) : (
                          <span className="shrink-0 rounded border border-red-500/40 bg-red-500/15 px-1 py-0.5 text-ui-xs text-red-300">
                            unmet
                          </span>
                        )
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

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
  onDuplicateTask?: (taskId: string) => Promise<void>;
}

export function ComputeJobsPanel({ projectId, onNavigateToTask, onLaunchCompute, onDuplicateTask }: ComputeJobsPanelProps) {
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [summary, setSummary] = useState<ComputeJobsSummary | null>(null);
  const [lifetimeSummary, setLifetimeSummary] = useState<ComputeJobsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [finishedWindowMinutes, setFinishedWindowMinutes] = useState(360);
  const [allTerminal, setAllTerminal] = useState(false);
  const [killingId, setKillingId] = useState<string | null>(null);
  const [analyzingId, setAnalyzingId] = useState<string | null>(null);
  const [analysisFeedback, setAnalysisFeedback] = useState<Record<string, { kind: 'running' | 'upgraded' | 'analysis_unavailable' | 'error'; message: string }>>({});
  const [analysisDocs, setAnalysisDocs] = useState<Record<string, KnowledgeDocument>>({});
  const [knowledgeDocs, setKnowledgeDocs] = useState<Record<string, KnowledgeDocument>>({});
  const [expandedDocIds, setExpandedDocIds] = useState<Set<string>>(new Set());
  const [nowMs, setNowMs] = useState(() => Date.now());

  const toggleDocExpanded = useCallback((key: string) => {
    setExpandedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }, []);

  const load = useCallback(
    async (signal?: AbortSignal, appendOffset?: number) => {
      if (!projectId) return;
      try {
        const currentOffset = appendOffset ?? 0;
        const result = await fetchComputeJobsPaged(
          projectId,
          signal,
          allTerminal
            ? { allTerminal: true, offset: currentOffset }
            : { includeFinishedMinutes: finishedWindowMinutes, offset: currentOffset },
        );
        if (currentOffset > 0) {
          setJobs((prev) => [...prev, ...result.jobs]);
        } else {
          setJobs(result.jobs);
          if (result.summary) setSummary(result.summary);
        }
        setHasMore(result.hasMore);
        setError(null);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        setError(e instanceof Error ? e.message : 'Failed to load compute jobs');
      } finally {
        setLoaded(true);
      }
    },
    [projectId, finishedWindowMinutes, allTerminal],
  );

  const handleLoadMore = useCallback(async () => {
    await load(undefined, jobs.length);
  }, [load, jobs.length]);

  usePoller((signal) => load(signal), {
    intervalMs: allTerminal ? 300_000 : POLL_INTERVAL_MS,
    enabled: !!projectId,
    deps: [projectId, finishedWindowMinutes, allTerminal],
  });

  // Fetch lifetime aggregate summary once per project (unbounded lifetime totals).
  useEffect(() => {
    if (!projectId) return;
    let aborted = false;
    Promise.resolve(fetchComputeJobsAggregateSummary(projectId)).then((summary) => {
      if (!aborted) setLifetimeSummary(summary);
    }).catch(() => {
      // Non-fatal: lifetime summary is informational, doesn't block the panel
    });
    return () => { aborted = true; };
  }, [projectId]);

  // Auto-fetch durable knowledge docs for terminal jobs that have a knowledge_document_id.
  useEffect(() => {
    if (!projectId) return;
    for (const job of jobs) {
      const docId = job.knowledge_document_id;
      if (!docId || isComputeJobActive(job) || knowledgeDocs[job.id]) continue;
      fetchKnowledgeDocument(projectId, docId).then((doc) => {
        setKnowledgeDocs((prev) => ({ ...prev, [job.id]: doc }));
      }).catch(() => {/* non-fatal */});
    }
  }, [jobs, projectId, knowledgeDocs]);

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

  if (!projectId) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-500 text-ui-sm">
        Select a project to view compute experiments
      </div>
    );
  }
  const showEmptyState = loaded && !error && jobs.length === 0;

  return (
    <div className="mt-4 rounded-xl border border-neutral-800 bg-neutral-950/60 p-4" data-viewport-allow-truncation>
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2 text-ui-md font-bold text-neutral-400 uppercase tracking-widest">
          <Cpu className="w-3.5 h-3.5 shrink-0" />
          Compute jobs
        </div>
        <div className="flex items-center gap-0.5" data-testid="compute-window-selector">
          {WINDOW_OPTIONS.map(({ label, minutes }) => (
            <button
              key={minutes}
              type="button"
              onClick={() => { setAllTerminal(false); setFinishedWindowMinutes(minutes); setHasMore(false); }}
              className={`min-h-[44px] min-w-[44px] rounded px-1.5 py-0.5 text-ui-xs font-semibold transition-colors ${!allTerminal && finishedWindowMinutes === minutes ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300'}`}
              data-testid={`compute-window-${label}`}
            >
              {label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => { setAllTerminal(true); setHasMore(false); }}
            className={`min-h-[44px] min-w-[44px] rounded px-1.5 py-0.5 text-ui-xs font-semibold transition-colors ${allTerminal ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300'}`}
            data-testid="compute-window-all"
          >
            All
          </button>
        </div>
      </div>

      {lifetimeSummary && lifetimeSummary.total_jobs > 0 && (
        <div
          className="mb-2 flex flex-wrap gap-x-4 gap-y-0.5 text-ui-sm font-mono text-neutral-400"
          data-testid="compute-lifetime-summary"
        >
          <span className="text-neutral-500 text-xs uppercase tracking-widest">Lifetime</span>
          <span>
            <span className="text-neutral-500">total</span> {lifetimeSummary.total_jobs}
          </span>
          {lifetimeSummary.total_spend_usd > 0 && (
            <span>
              <span className="text-neutral-500">spend</span> {fmtUsd(lifetimeSummary.total_spend_usd)}
            </span>
          )}
          {lifetimeSummary.criterion_measured > 0 && (
            <span>
              <span className="text-neutral-500">criterion</span>{' '}
              {lifetimeSummary.criterion_met}/{lifetimeSummary.criterion_measured} met
            </span>
          )}
        </div>
      )}

      {summary && summary.total_jobs > 0 && (
        <div
          className="mb-3 flex flex-wrap gap-x-4 gap-y-0.5 text-ui-sm font-mono text-neutral-400"
          data-testid="compute-jobs-summary"
        >
          <span className="text-neutral-500 text-xs uppercase tracking-widest">Windowed</span>
          <span>
            <span className="text-neutral-500">total</span> {summary.total_jobs}
          </span>
          {summary.active_jobs > 0 && (
            <span>
              <span className="text-neutral-500">active</span>{' '}
              <span className="text-sky-300">{summary.active_jobs}</span>
            </span>
          )}
          {summary.total_spend_usd > 0 && (
            <span>
              <span className="text-neutral-500">spend</span> {fmtUsd(summary.total_spend_usd)}
            </span>
          )}
          {summary.criterion_measured > 0 && (
            <span>
              <span className="text-neutral-500">criterion</span>{' '}
              {summary.criterion_met}/{summary.criterion_measured} met
            </span>
          )}
        </div>
      )}

      {error && (
        <p className="text-ui-sm text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 mb-2 break-words flex items-start gap-1.5">
          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          {error}
        </p>
      )}

      {showEmptyState && (
        <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/40 px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-semibold text-neutral-200">No compute jobs</div>
              <div className="mt-0.5 text-ui-sm text-neutral-500">
                Launch a structured experiment for this project.
              </div>
            </div>
            {onLaunchCompute && (
              <button
                type="button"
                onClick={onLaunchCompute}
                className="shrink-0 min-h-[36px] rounded bg-sky-500/20 px-3 text-ui-md font-semibold text-sky-200 transition-colors hover:bg-sky-500/30"
              >
                Launch compute experiment
              </button>
            )}
          </div>
        </div>
      )}

      <ExperimentSweepSummary jobs={jobs} />

      <div className="flex flex-col gap-2" data-testid="compute-jobs-list">
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
                  <div className="text-ui-sm text-neutral-500 font-mono mt-0.5 break-words">
                    {job.backend}
                    {job.machine_type ? ` · ${job.machine_type}` : ''}
                    {job.accelerator_type ? ` · ${job.accelerator_type} x${job.accelerator_count ?? 1}` : ''}
                    {job.launch_contract?.provisioning_model ? ` · ${job.launch_contract.provisioning_model}` : ''}
                    {job.external_compute_id ? ` · ${job.external_compute_id}` : ''}
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
                  className={`shrink-0 rounded border px-1.5 py-0.5 text-ui-sm font-semibold ${statusBadgeClass(job)}`}
                >
                  {active ? job.status : computeJobTerminalLabel(job)}
                </span>
              </div>

              <div className="mb-1.5">
                <div className="flex items-center justify-between text-ui-sm text-neutral-400 font-mono mb-1">
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

              {(() => {
                if (!active) return null;
                const burnRate = computeJobBurnRate(job, nowMs);
                if (burnRate == null) return null;
                const projected = computeJobProjectedSpend(job, burnRate, nowMs);
                const cap = job.spend_cap_usd ?? 0;
                const willExceedCap = projected != null && cap > 0 && projected > cap;
                return (
                  <>
                    <div className="mb-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm text-neutral-400 font-mono" data-testid="compute-burn-rate">
                      <span>
                        <span className="text-neutral-500">rate</span>{' '}
                        {fmtUsd(burnRate)}/hr
                      </span>
                      {projected != null && (
                        <span>
                          <span className="text-neutral-500">projected</span>{' '}
                          {fmtUsd(projected)}
                        </span>
                      )}
                    </div>
                    {willExceedCap && (
                      <div
                        className="mb-1.5 rounded border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-ui-sm text-red-200"
                        data-testid="compute-budget-kill-warning"
                      >
                        <div className="flex items-start gap-1.5">
                          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                          <span>
                            At current burn rate ({fmtUsd(burnRate)}/hr), this job will hit the spend cap ({fmtUsd(cap)}) before its deadline and be killed.
                          </span>
                        </div>
                      </div>
                    )}
                  </>
                );
              })()}
              {(() => {
                if (active) return null;
                const runtimeSec = computeJobRuntimeSec(job);
                const deadlineSec = job.launch_contract?.requested_deadline_seconds;
                const costRate =
                  runtimeSec != null && runtimeSec > 0 && job.final_cost_usd != null
                    ? job.final_cost_usd / (runtimeSec / 3600)
                    : null;
                if (runtimeSec === null && costRate === null) return null;
                return (
                  <div
                    className="mb-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm text-neutral-400 font-mono"
                    data-testid="compute-result-story"
                  >
                    {runtimeSec !== null && (
                      <span>
                        <span className="text-neutral-500">ran</span>{' '}
                        {fmtDuration(runtimeSec)}
                        {typeof deadlineSec === 'number' && (
                          <span className="text-neutral-500"> / {fmtDuration(deadlineSec)} budget</span>
                        )}
                      </span>
                    )}
                    {costRate !== null && (
                      <span>
                        <span className="text-neutral-500">rate</span>{' '}
                        {fmtUsd(costRate)}/hr
                      </span>
                    )}
                  </div>
                );
              })()}
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-ui-sm text-neutral-400 font-mono min-w-0">
                  <Clock className="w-3 h-3 shrink-0" />
                  {active ? (
                    <span className="break-words">{fmtCountdown(job.wall_clock_deadline, nowMs)} left</span>
                  ) : (
                    <span className="break-words">{job.terminal_reason ?? '—'}</span>
                  )}
                  {!active && typeof (job.polling_metadata?.exit_code) === 'number' && (
                    <span
                      className="shrink-0 rounded border border-neutral-700 bg-neutral-800/60 px-1 py-0.5 text-ui-xs text-neutral-400"
                      data-testid="compute-exit-code"
                    >
                      exit {job.polling_metadata.exit_code as number}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {!active && (
                    <>
                      <button
                        onClick={() => void handleAnalyze(job)}
                        disabled={analyzingId === job.id}
                        className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-violet-500/20 text-violet-200 hover:bg-violet-500/30 text-ui-md font-semibold px-3 transition-colors disabled:opacity-50"
                        title="Run analysis for this terminal compute job"
                      >
                        <Cpu className="w-3 h-3" />
                        {analyzingId === job.id ? 'Analyzing…' : 'Run analysis'}
                      </button>
                      {onDuplicateTask && (
                        <button
                          onClick={() => job.task_id && void onDuplicateTask(job.task_id)}
                          className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-sky-500/20 text-sky-200 hover:bg-sky-500/30 text-ui-md font-semibold px-3 transition-colors"
                          title="Re-run this experiment with the same configuration"
                        >
                          <Cpu className="w-3 h-3" />
                          Re-run
                        </button>
                      )}
                    </>
                  )}
                  {active && (
                    <button
                      onClick={() => void handleKill(job)}
                      disabled={killingId === job.id}
                      className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-red-500/20 text-red-200 hover:bg-red-500/30 text-ui-md font-semibold px-3 transition-colors disabled:opacity-50"
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
                    className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-ui-sm text-amber-200"
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
              {(() => {
                const doc = knowledgeDocs[job.id];
                if (!doc) return null;
                const docKey = `knowledge-${job.id}`;
                const expanded = expandedDocIds.has(docKey);
                return (
                  <div
                    className="mt-1.5 rounded border border-sky-500/30 bg-sky-500/10 px-2 py-1.5 text-ui-sm"
                    data-testid="compute-knowledge-doc-link"
                  >
                    <div className="flex items-start gap-1.5">
                      <FileText className="w-3 h-3 shrink-0 mt-0.5 text-sky-300" />
                      <div className="min-w-0 flex-1">
                        <button
                          type="button"
                          onClick={() => toggleDocExpanded(docKey)}
                          className="font-semibold text-sky-200 break-words text-left w-full hover:text-sky-100"
                          data-testid="compute-knowledge-doc-toggle"
                        >
                          {doc.title}
                        </button>
                        {!expanded && doc.summary && (
                          <div className="mt-0.5 text-sky-300/80 break-words whitespace-pre-wrap line-clamp-3">{doc.summary}</div>
                        )}
                        {expanded && (
                          <div className="mt-0.5 text-sky-300/80 break-words whitespace-pre-wrap" data-testid="compute-knowledge-doc-body">
                            {doc.body ?? doc.summary}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })()}
              {analysisFeedback[job.id] && (
                <div
                  className={`mt-1.5 rounded border px-2 py-1 text-ui-sm ${
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
                const docKey = `analysis-${job.id}`;
                const expanded = expandedDocIds.has(docKey);
                return (
                  <div
                    className="mt-1.5 rounded border border-violet-500/30 bg-violet-500/10 px-2 py-1.5 text-ui-sm"
                    data-testid="compute-analysis-doc"
                  >
                    <div className="flex items-start gap-1.5">
                      <FileText className="w-3 h-3 shrink-0 mt-0.5 text-violet-300" />
                      <div className="min-w-0 flex-1">
                        <button
                          type="button"
                          onClick={() => toggleDocExpanded(docKey)}
                          className="font-semibold text-violet-200 break-words text-left w-full hover:text-violet-100"
                          data-testid="compute-analysis-doc-toggle"
                        >
                          {doc.title}
                        </button>
                        {!expanded && doc.summary && (
                          <div className="mt-0.5 text-violet-300/80 break-words whitespace-pre-wrap line-clamp-3">{doc.summary}</div>
                        )}
                        {!expanded && !doc.summary && doc.body && (
                          <div className="mt-0.5 text-violet-300/80 break-words line-clamp-4 whitespace-pre-wrap">
                            {doc.body.slice(0, 400)}{doc.body.length > 400 ? '…' : ''}
                          </div>
                        )}
                        {expanded && (
                          <div className="mt-0.5 text-violet-300/80 break-words whitespace-pre-wrap" data-testid="compute-analysis-doc-body">
                            {doc.body ?? doc.summary}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })()}
              {job.artifact_id && (
                <div className="mt-1.5 flex items-start gap-1.5 text-ui-sm">
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
                <div className="mt-1.5 flex items-start gap-1.5 text-ui-sm text-neutral-400 font-mono break-words">
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
                const scalarEntries = Object.entries(metrics).filter(([, v]) => v === null || typeof v !== 'object');
                const arms = normalizeArms(metrics.arms);
                if (scalarEntries.length === 0 && !arms) return null;
                return (
                  <>
                    {scalarEntries.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm text-neutral-400 font-mono">
                        {scalarEntries.map(([k, v]) => (
                          <span key={k}>
                            <span className="text-neutral-500">{k}:</span> {String(v)}
                          </span>
                        ))}
                      </div>
                    )}
                    {arms && (
                      <div className="mt-1.5" data-testid="arms-breakdown">
                        <div className="text-ui-sm text-neutral-500 font-mono mb-0.5">Arms</div>
                        <div className="flex flex-col gap-1">
                          {arms.map((arm) => {
                            const exitCodeVal = typeof arm.metrics.exit_code === 'number' ? arm.metrics.exit_code : null;
                            const errorVal = typeof arm.metrics.error === 'string' ? arm.metrics.error : typeof arm.metrics.reason === 'string' ? arm.metrics.reason : null;
                            const stderrVal = typeof arm.metrics.stderr_excerpt === 'string' ? arm.metrics.stderr_excerpt : null;
                            const isFailed = (exitCodeVal !== null && exitCodeVal !== 0) || errorVal !== null;
                            const numericEntries = Object.entries(arm.metrics).filter(([mk, mv]) => typeof mv === 'number' && !DIAGNOSTIC_KEYS.has(mk));
                            const labelVal = typeof arm.metrics.label === 'string' ? arm.metrics.label : null;
                            const noteVal = typeof arm.metrics.note === 'string' ? arm.metrics.note : null;
                            const logVal = typeof arm.metrics.log === 'string' ? arm.metrics.log : null;
                            const otherStrings = Object.entries(arm.metrics).filter(
                              ([mk, mv]) => typeof mv === 'string' && !DIAGNOSTIC_KEYS.has(mk),
                            );
                            return (
                              <div
                                key={arm.name}
                                className={`rounded border px-2 py-1 ${isFailed ? 'border-red-500/30 bg-red-500/10' : 'border-neutral-700/50 bg-neutral-800/40'}`}
                              >
                                <div className="flex items-center gap-1.5 flex-wrap">
                                  <div className={`text-ui-sm font-semibold font-mono ${isFailed ? 'text-red-300' : 'text-neutral-300'}`}>{arm.name}</div>
                                  {exitCodeVal !== null && (
                                    <span
                                      className={`rounded border px-1 py-0.5 text-ui-xs ${isFailed ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-neutral-700 bg-neutral-800/60 text-neutral-400'}`}
                                      data-testid="arm-exit-code"
                                    >
                                      exit {exitCodeVal}
                                    </span>
                                  )}
                                </div>
                                {labelVal && (
                                  <div className="text-ui-sm text-neutral-400 italic mt-0.5" data-testid="arm-label">{labelVal}</div>
                                )}
                                {numericEntries.length > 0 && (
                                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm text-neutral-400 font-mono mt-0.5">
                                    {numericEntries.map(([mk, mv]) => (
                                      <span key={mk}>
                                        <span className="text-neutral-500">{mk}:</span> {String(mv)}
                                      </span>
                                    ))}
                                  </div>
                                )}
                                {otherStrings.map(([mk, mv]) => (
                                  <div key={mk} className="mt-0.5 text-ui-sm text-neutral-400 font-mono">
                                    <span className="text-neutral-500">{mk}:</span> {String(mv)}
                                  </div>
                                ))}
                                {errorVal && (
                                  <div className="mt-0.5 text-ui-sm text-red-300/90 break-words font-mono" data-testid="arm-error">
                                    {errorVal.slice(0, 120)}{errorVal.length > 120 ? '…' : ''}
                                  </div>
                                )}
                                {noteVal && (
                                  <div className="mt-0.5 text-ui-sm text-neutral-400 break-words" data-testid="arm-note">{noteVal}</div>
                                )}
                                {logVal && (
                                  <pre className="mt-0.5 rounded border border-neutral-800 bg-neutral-950 px-2 py-1 text-ui-xs text-neutral-300 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-24 overflow-y-auto" data-testid="arm-log">{logVal}</pre>
                                )}
                                {stderrVal && (
                                  <pre className="mt-0.5 rounded border border-neutral-800 bg-neutral-950 px-2 py-1 text-ui-xs text-red-200 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-24 overflow-y-auto" data-testid="arm-stderr">{stderrVal}</pre>
                                )}
                              </div>
                            );
                          })}
                        </div>
                        {(() => {
                          const cv = job.criterion_verdict;
                          if (!cv?.metric) return null;
                          const dir: MetricDirection = cv.operator === '>=' || cv.operator === '>' ? 'higher_better' : 'lower_better';
                          const verdict = armComparisonVerdict(arms, cv.metric, dir);
                          if (!verdict) return null;
                          return (
                            <div
                              className="mt-1.5 flex items-center gap-1.5 text-ui-sm font-mono flex-wrap"
                              data-testid="arm-comparison-verdict"
                            >
                              <span className="text-neutral-500 shrink-0">Winner</span>
                              <span className="font-semibold text-emerald-300">{verdict.winnerName}</span>
                              <span className="text-neutral-400">{cv.metric}={verdict.winnerValue}</span>
                              <span
                                className="rounded border border-emerald-500/40 bg-emerald-500/15 px-1 py-0.5 text-ui-xs font-semibold text-emerald-300"
                                data-testid="arm-comparison-margin"
                              >
                                +{verdict.margin.toFixed(2)} vs {verdict.runnerUpName}
                              </span>
                            </div>
                          );
                        })()}
                      </div>
                    )}
                  </>
                );
              })()}
              {(() => {
                if (active) return null;
                const { experiment, benchmark, hypothesis } = job;
                if (!experiment && !benchmark && !hypothesis) return null;
                return (
                  <div
                    className="mt-1.5 flex flex-col gap-0.5 text-ui-sm font-mono"
                    data-testid="compute-experiment-identity"
                  >
                    {(experiment || benchmark) && (
                      <div className="flex items-start gap-1.5">
                        <span className="text-neutral-500 shrink-0">{experiment ? 'Experiment' : 'Benchmark'}</span>
                        <span className="text-neutral-300 break-words">{experiment ?? benchmark}</span>
                      </div>
                    )}
                    {hypothesis && (
                      <div className="flex items-start gap-1.5">
                        <span className="text-neutral-500 shrink-0">Hypothesis</span>
                        <span className="text-neutral-300 break-words">{hypothesis}</span>
                      </div>
                    )}
                  </div>
                );
              })()}
              {(() => {
                if (active) return null;
                const sc = job.success_criterion;
                const verdict = job.criterion_verdict;
                if (!sc) return null;
                const expr = `${sc.metric} ${sc.operator} ${sc.threshold}`;
                const margin = computeJobMargin(job);
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
                    className="mt-1.5 flex flex-col gap-1 text-ui-sm font-mono"
                    data-testid="criterion-verdict-row"
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="text-neutral-500 shrink-0">Criterion</span>
                      <span className="text-neutral-300 break-words">{expr}</span>
                      {verdict && verdict.value !== null && verdict.value !== undefined && (
                        <span className="text-neutral-500">
                          (measured: {verdict.value})
                        </span>
                      )}
                      {chip}
                    </div>
                    {margin !== null && (
                      <div className="flex items-center gap-1.5 text-neutral-500">
                        <span className="shrink-0">Margin</span>
                        <span
                          className={`font-semibold ${
                            margin >= 0 ? 'text-emerald-300' : 'text-red-300'
                          }`}
                          data-testid="criterion-margin"
                        >
                          {margin >= 0 ? '+' : ''}{margin.toFixed(2)}
                        </span>
                      </div>
                    )}
                  </div>
                );
              })()}
              {(() => {
                if (!active) return null;
                const lo = job.polling_metadata?.live_output as ComputeJobLiveOutput | undefined | null;
                if (!lo || typeof lo.text !== 'string' || !lo.text.trim()) return null;
                const lines = lo.text.split('\n');
                const displayLines = lines.length > 30 ? lines.slice(-30) : lines;
                const clipped = lines.length > 30;
                return (
                  <div className="mt-1.5" data-testid="compute-live-output">
                    <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
                      <span>Live output</span>
                      <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-neutral-500">
                        updating…
                      </span>
                      {(lo.truncated || clipped) && (
                        <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-neutral-400">
                          truncated
                        </span>
                      )}
                    </div>
                    <pre className="rounded border border-neutral-800 bg-neutral-950 px-2 py-1.5 text-ui-xs text-neutral-300 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
                      {displayLines.join('\n')}
                    </pre>
                  </div>
                );
              })()}
              {(() => {
                if (active) return null;
                const cl = job.polling_metadata?.command_log as ComputeJobCommandLog | undefined | null;
                if (!cl || typeof cl.text !== 'string' || !cl.text.trim()) return null;
                const lines = cl.text.split('\n');
                const clipped = lines.length > 30;
                const logKey = `cmdlog-${job.id}`;
                const logExpanded = expandedDocIds.has(logKey);
                const displayLines = clipped && !logExpanded ? lines.slice(-30) : lines;
                return (
                  <div className="mt-1.5" data-testid="compute-command-log">
                    <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
                      <span>Command log</span>
                      {cl.truncated && (
                        <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-neutral-400">
                          server-truncated
                        </span>
                      )}
                      {clipped && !logExpanded && (
                        <button
                          type="button"
                          onClick={() => toggleDocExpanded(logKey)}
                          className="min-h-[44px] rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-sky-400 hover:text-sky-300"
                          data-testid="compute-log-show-all"
                        >
                          Show all ({lines.length} lines)
                        </button>
                      )}
                    </div>
                    <pre className="rounded border border-neutral-800 bg-neutral-950 px-2 py-1.5 text-ui-xs text-neutral-300 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
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
                    className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-ui-sm text-amber-200"
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

      {hasMore && (
        <div className="mt-2 flex justify-center">
          <button
            type="button"
            onClick={() => void handleLoadMore()}
            className="min-h-[44px] rounded bg-neutral-800 px-4 text-ui-sm font-semibold text-neutral-300 hover:bg-neutral-700 transition-colors"
            data-testid="compute-load-more"
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}

export default ComputeJobsPanel;
