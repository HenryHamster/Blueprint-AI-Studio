import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Cpu, Square, AlertTriangle, Clock, ExternalLink, Package, FileText, LayoutGrid, CheckCircle, XCircle, BookOpen, Search, List } from 'lucide-react';
import { analyzeComputeJob, askComputeJob, fetchAwaitingLocalRunnerHolds, fetchComputeJobsPaged, fetchComputeJobsAggregateSummary, fetchComputeSweepGroups, fetchHeldComputeExperiments, fetchKnowledgeDocument, killComputeJob, rerunComputeExperiment, type AwaitingLocalRunnerHold, type ComputeJob, type ComputeJobCommandLog, type ComputeJobLiveOutput, type ComputeJobsSummary, type ComputeSweepGroupsResponse, type ComputeSuccessOperator, type HeldComputeExperiment, type KnowledgeDocument } from '../api';
import { type ComputeExperimentForm, defaultComputeExperimentForm } from '../hooks/useTaskModal';
import { usePersistedState } from '../hooks/usePersistedState';
import { usePoller } from '../hooks/usePoller';
import { ResolvedReferenceLink } from './TaskCard';
import { ConfirmDialog } from './ConfirmDialog';
import { ReleaseEvidenceChip } from './ReleaseEvidence';
import { asLiveReleaseEvidence } from './ReleaseEvidence.utils';
import { AcceptanceGradeBadge } from './AcceptanceGradeBadge';
import { EvidenceTierBadge, MutableImageBadge } from './EvidenceTierBadge';
import { ComputeArtifactGallery } from './ComputeArtifactGallery';
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
  computeJobExecutionLocation,
  computeJobEvidenceTier,
  computeJobEvidenceGroup,
  computeJobEvidenceTierRank,
  computeJobImagePinStatus,
  fmtUsd,
  fmtCountdown,
  fmtDuration,
} from './ComputeJobsPanel.utils';

const POLL_INTERVAL_MS = 10_000;

const DIAGNOSTIC_KEYS = new Set(['exit_code', 'error', 'reason', 'stderr_excerpt', 'label', 'note', 'log', 'elapsed_seconds']);

const WINDOW_OPTIONS: { label: string; minutes: number }[] = [
  { label: '6h', minutes: 360 },
  { label: '24h', minutes: 1440 },
  { label: '3d', minutes: 4320 },
  { label: '7d', minutes: 10080 },
];

type AskStateValue = {
  question: string;
  submitting: boolean;
  thread: Array<{ question: string; result: { answer: string | null; citations: string[]; grounding_miss: boolean; fallback_reason: string | null } }>;
  error: string | null;
};

function sanitizeAskState(parsed: unknown): Record<string, AskStateValue> {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
  const obj = parsed as Record<string, unknown>;
  const result: Record<string, AskStateValue> = {};
  for (const [jobId, state] of Object.entries(obj)) {
    if (!state || typeof state !== 'object' || Array.isArray(state)) continue;
    const s = state as Record<string, unknown>;
    result[jobId] = {
      question: typeof s.question === 'string' ? s.question : '',
      submitting: false,
      thread: Array.isArray(s.thread) ? s.thread : [],
      error: s.error === null || typeof s.error === 'string' ? s.error : null,
    };
  }
  return result;
}

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

function criterionMet(value: number, operator: string, threshold: number): boolean {
  if (operator === '>=') return value >= threshold;
  if (operator === '>') return value > threshold;
  if (operator === '<=') return value <= threshold;
  if (operator === '<') return value < threshold;
  if (operator === '==') return value === threshold;
  if (operator === '!=') return value !== threshold;
  return false;
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
              className={`rounded border px-1.5 py-0.5 text-ui-xs font-semibold ${v?.met ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : v && !v.measurable ? 'border-amber-500/40 bg-amber-500/15 text-amber-300' : v ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-neutral-600/40 bg-neutral-800/60 text-neutral-400'}`}
              data-testid="matrix-verdict-chip"
            >
              {(job.task_title ?? job.id).slice(0, 20)}: {v?.met ? 'met' : v && !v.measurable ? 'inconclusive' : v ? 'missed' : 'no verdict'}
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
              const perArm = job.criterion_verdict?.per_arm;
              const cvOperator = job.criterion_verdict?.operator;
              const cvThreshold = job.criterion_verdict?.threshold;
              const hasArmsVerdict = perArm != null && Object.keys(perArm).length > 0;
              let armsMetCount = 0;
              if (hasArmsVerdict && cvOperator && cvThreshold != null) {
                for (const arm of arms) {
                  const pav = perArm[arm.name];
                  if (pav != null && criterionMet(pav, cvOperator, cvThreshold)) {
                    armsMetCount++;
                  }
                }
              }
              return (
                <div key={job.id} className="mb-1.5">
                  <div className="text-ui-sm font-semibold text-neutral-300 font-mono mb-0.5">
                    {(job.task_title ?? job.id).slice(0, 24)}
                    {hasArmsVerdict && (
                      <span className="text-ui-xs text-neutral-500 font-normal ml-2" data-testid="arms-met-count">
                        {armsMetCount}/{arms.length} arms met
                      </span>
                    )}
                  </div>
                  {arms.map((arm) => {
                    const armExitCode = typeof arm.metrics.exit_code === 'number' ? arm.metrics.exit_code : null;
                    const armError = typeof arm.metrics.error === 'string' ? arm.metrics.error : typeof arm.metrics.reason === 'string' ? arm.metrics.reason : null;
                    const armFailed = (armExitCode !== null && armExitCode !== 0) || armError !== null;
                    const armCostUsd = typeof arm.metrics.cost_usd === 'number' ? arm.metrics.cost_usd as number : null;
                    let armPassFail: 'pass' | 'fail' | 'inconclusive' | null = null;
                    if (perArm && arm.name in perArm && cvOperator && cvThreshold != null) {
                      const pav = perArm[arm.name];
                      if (pav == null) {
                        armPassFail = 'inconclusive';
                      } else {
                        armPassFail = criterionMet(pav, cvOperator, cvThreshold) ? 'pass' : 'fail';
                      }
                    }
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
                        {armCostUsd !== null && (
                          <span
                            className="rounded border border-neutral-700/50 bg-neutral-800/40 px-1 py-0.5 text-ui-xs text-neutral-300"
                            data-testid="arm-cost-usd"
                          >
                            {fmtUsd(armCostUsd)}
                          </span>
                        )}
                        {armPassFail !== null && (
                          <span
                            className={`rounded border px-1 py-0.5 text-ui-xs font-semibold ${
                              armPassFail === 'pass'
                                ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                                : armPassFail === 'inconclusive'
                                ? 'border-amber-500/40 bg-amber-500/15 text-amber-300'
                                : 'border-red-500/40 bg-red-500/15 text-red-300'
                            }`}
                            data-testid="arm-passfail-chip"
                          >
                            {armPassFail}
                          </span>
                        )}
                        {Object.entries(arm.metrics).filter(([mk]) => !DIAGNOSTIC_KEYS.has(mk) && mk !== 'cost_usd').map(([mk, mv]) => (
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

function SweepKnowledgeGroupsSection({
  sweepGroups,
  onNavigateToKnowledge,
}: {
  sweepGroups: ComputeSweepGroupsResponse['sweep_groups'];
  onNavigateToKnowledge?: (documentId: string) => void;
}) {
  const [compareSet, setCompareSet] = useState<Set<string>>(new Set());
  const entries = Object.entries(sweepGroups);
  if (entries.length === 0) return null;

  const verdictLabel = (met: boolean | null | undefined) => met === true ? 'MET' : met === false ? 'NOT MET' : 'UNMEASURED';
  const gradeLabel = (grade: string | null | undefined) => grade === 'acceptance' ? 'accepted' : grade === 'measured_only' ? 'measured' : '—';
  const provenanceLabel = (doc: ComputeSweepGroupsResponse['sweep_groups'][string][number]) => {
    const backend = doc.run_provenance?.executing_backend;
    if (!backend) return null;
    const { label } = computeJobExecutionLocation(backend);
    const host = doc.run_provenance?.runner_hostname ?? doc.run_provenance?.runner_target_id;
    return host ? `${label} · ${host}` : label;
  };
  const armEvidence = (doc: ComputeSweepGroupsResponse['sweep_groups'][string][number]) => {
    const verdicts = doc.per_arm_verdicts ? Object.entries(doc.per_arm_verdicts) : [];
    if (verdicts.length === 0) return doc.criterion?.metric ?? '—';
    return verdicts
      .map(([arm, v]) => `${arm}: ${v.value ?? 'n/a'} ${verdictLabel(v.met)}${v.cost_usd != null ? ` (${fmtUsd(v.cost_usd)})` : ''}`)
      .join(' · ');
  };

  function toggleCompare(sweepId: string) {
    setCompareSet((prev) => {
      const next = new Set(prev);
      if (next.has(sweepId)) next.delete(sweepId); else next.add(sweepId);
      return next;
    });
  }

  return (
    <div className="mb-3 flex flex-col gap-2" data-testid="sweep-knowledge-groups">
      <div className="text-ui-xs font-semibold text-neutral-500 uppercase tracking-widest">
        Sweep comparisons (knowledge)
      </div>
      {entries.map(([sweepId, docs]) => {
        const comparing = compareSet.has(sweepId);
        const label = docs[0]?.title ?? sweepId;
        return (
          <div
            key={sweepId}
            className="rounded-lg border border-indigo-500/20 bg-indigo-500/5 px-3 py-2"
            data-testid="sweep-knowledge-group"
          >
            <div className="flex items-center justify-between gap-2 mb-1.5">
              <span className="text-ui-sm font-semibold text-indigo-300 break-words min-w-0" data-testid="sweep-knowledge-group-label">
                {label}
              </span>
              <div className="flex items-center gap-2 shrink-0">
                {!comparing && (
                  <span className="text-ui-sm text-neutral-400 font-mono">
                    {docs.length} runs
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => toggleCompare(sweepId)}
                  className="min-h-[44px] min-w-[44px] flex items-center gap-1 rounded px-1.5 py-0.5 text-ui-xs font-semibold text-indigo-400 hover:text-indigo-200 hover:bg-indigo-500/15 transition-colors"
                  data-testid="sweep-knowledge-compare-toggle"
                >
                  <LayoutGrid className="w-3 h-3" />
                  {comparing ? 'List' : 'Compare'}
                </button>
              </div>
            </div>
            {comparing ? (
              <div className="overflow-x-auto">
                <table className="w-full text-ui-sm font-mono" data-testid="sweep-knowledge-comparison-table">
                  <thead>
                    <tr>
                      <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Run</th>
                      <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Verdict</th>
                      <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Tier / Provenance</th>
                      <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Criterion</th>
                      <th className="text-left pr-3 pb-1 text-neutral-500 font-normal">Evidence</th>
                      <th className="text-left pb-1 text-neutral-500 font-normal">Summary</th>
                    </tr>
                  </thead>
                  <tbody>
                    {docs.map((doc) => (
                      <tr key={doc.id} className="border-t border-neutral-800/50">
                        <td className="py-0.5 pr-3 text-neutral-300 max-w-32 truncate">
                          {onNavigateToKnowledge ? (
                            <button
                              type="button"
                              onClick={() => onNavigateToKnowledge(doc.id)}
                              className="text-sky-300 hover:text-sky-200 hover:underline text-left truncate max-w-full"
                            >
                              {(doc.arm_label ?? doc.title).slice(0, 28)}
                            </button>
                          ) : (
                            <span className="truncate">{(doc.arm_label ?? doc.title).slice(0, 28)}</span>
                          )}
                        </td>
                        <td className="py-0.5 pr-3 text-neutral-400">{verdictLabel(doc.criterion_met)} / {gradeLabel(doc.compute_acceptance_grade)}</td>
                        <td className="py-0.5 pr-3" data-testid="sweep-knowledge-tier-cell">
                          <div className="flex flex-col gap-0.5">
                            <EvidenceTierBadge tier={doc.evidence_tier} />
                            {provenanceLabel(doc) && (
                              <span className="text-neutral-500 text-ui-xs truncate max-w-36" data-testid="sweep-knowledge-provenance-label">
                                {provenanceLabel(doc)}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="py-0.5 pr-3 text-neutral-400">{doc.criterion ? `${doc.criterion.metric} ${doc.criterion.operator} ${doc.criterion.threshold}` : '—'}</td>
                        <td className="py-0.5 pr-3 text-neutral-400 max-w-xs truncate">{armEvidence(doc)}</td>
                        <td className="py-0.5 text-neutral-400 max-w-xs truncate">{doc.summary?.slice(0, 80) ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="flex flex-col gap-0.5">
                {docs.map((doc) => (
                  <div key={doc.id} className="flex items-center gap-2 text-ui-sm font-mono text-neutral-400">
                    <span className="min-w-0 flex-1 truncate">
                      {onNavigateToKnowledge ? (
                        <button
                          type="button"
                          onClick={() => onNavigateToKnowledge(doc.id)}
                          className="text-sky-300 hover:text-sky-200 hover:underline text-left truncate"
                        >
                          {doc.arm_label ?? doc.title}
                        </button>
                      ) : (
                        doc.arm_label ?? doc.title
                      )}
                    </span>
                    <EvidenceTierBadge tier={doc.evidence_tier} />
                    {doc.criterion_met !== undefined && (
                      <span className={`shrink-0 rounded border px-1 py-0.5 text-ui-xs ${doc.criterion_met === true ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : doc.criterion_met === false ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-amber-500/40 bg-amber-500/15 text-amber-300'}`}>
                        {verdictLabel(doc.criterion_met)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
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
                        ) : !verdict.measurable ? (
                          <span className="shrink-0 rounded border border-amber-500/40 bg-amber-500/15 px-1 py-0.5 text-ui-xs text-amber-300">
                            inconclusive
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

function AwaitingLocalRunnerSection({
  holds,
  nowMs,
}: {
  holds: AwaitingLocalRunnerHold[];
  nowMs: number;
}) {
  if (!holds || holds.length === 0) return null;

  return (
    <div className="mb-3" data-testid="awaiting-local-runner-section">
      <div className="text-ui-sm font-semibold text-amber-300 uppercase tracking-widest mb-1.5 flex items-center gap-1.5">
        <AlertTriangle className="w-3 h-3 shrink-0" />
        Waiting for local runner
      </div>
      <div className="flex flex-col gap-1.5">
        {holds.map((hold) => {
          const capLabel = hold.spend_cap_usd != null ? ` · $${Number(hold.spend_cap_usd).toFixed(2)}` : '';
          const waitingMs = Date.parse(hold.awaiting_local_runner_since || hold.awaiting_forced_runner_since || '');
          const waitingSec = isNaN(waitingMs) ? null : Math.max(0, Math.round((nowMs - waitingMs) / 1000));
          return (
            <div
              key={hold.id}
              className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2"
              data-testid="awaiting-local-runner-row"
            >
              <div className="text-xs font-semibold text-amber-200 break-words">{hold.title ?? hold.id}{capLabel}</div>
              <div className="text-ui-sm text-amber-400 font-mono mt-0.5" data-testid="awaiting-local-runner-waiting">
                {hold.forced_runner?.hostname
                  ? <>waiting for forced runner <span className="font-semibold">{hold.forced_runner?.hostname}</span></>
                  : 'waiting for local runner'
                }
                {waitingSec != null && <> since {fmtDuration(waitingSec)} ago</>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function HeldExperimentsSection({
  experiments,
  onApprove,
  onDismiss,
}: {
  experiments: HeldComputeExperiment[];
  onApprove?: (taskId: string) => Promise<void>;
  onDismiss?: (taskId: string) => Promise<void>;
}) {
  const [actionId, setActionId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchApproving, setBatchApproving] = useState(false);

  if (!experiments || experiments.length === 0) return null;

  const handleApprove = async (id: string) => {
    if (!onApprove) return;
    setActionId(id);
    try { await onApprove(id); } finally { setActionId(null); }
  };

  const handleDismiss = async (id: string) => {
    if (!onDismiss) return;
    setActionId(id);
    try { await onDismiss(id); } finally { setActionId(null); }
  };

  const allSelected = experiments.length > 0 && selectedIds.size === experiments.length;

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(experiments.map(e => e.id)));
    }
  };

  const handleBatchApprove = async () => {
    if (!onApprove || selectedIds.size === 0) return;
    setBatchApproving(true);
    try {
      await Promise.all([...selectedIds].map(id => onApprove(id)));
      setSelectedIds(new Set());
    } finally {
      setBatchApproving(false);
    }
  };

  const hasSelection = selectedIds.size > 0;
  const busy = actionId !== null || batchApproving;

  return (
    <div className="mb-3" data-testid="held-experiments-section">
      <div className="text-ui-sm font-semibold text-amber-300 uppercase tracking-widest mb-1.5 flex items-center gap-1.5">
        <AlertTriangle className="w-3 h-3 shrink-0" />
        Held for approval
        <span className="text-ui-xs text-amber-400/70 font-mono normal-case font-normal">({experiments.length})</span>
      </div>
      <div className="flex flex-col gap-1.5">
        {/* Batch action toolbar — only when approve is wired and experiments are selectable */}
        {onApprove && experiments.length >= 1 && (
          <div className="flex items-center gap-2 mb-0.5">
            <label className="flex items-center gap-1.5 text-ui-sm text-neutral-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                disabled={busy}
                className="rounded border-neutral-600 bg-neutral-800 text-sky-500 focus:ring-sky-500 disabled:opacity-40"
                data-testid="held-experiment-select-all"
              />
              {allSelected ? `${experiments.length} selected` : 'Select all'}
            </label>
            {hasSelection && (
              <button
                type="button"
                onClick={() => void handleBatchApprove()}
                disabled={batchApproving}
                className="min-h-[28px] rounded bg-emerald-500/20 px-2.5 text-ui-sm font-semibold text-emerald-200 hover:bg-emerald-500/30 transition-colors disabled:opacity-50"
                data-testid="held-experiment-batch-approve"
              >
                {batchApproving ? 'Approving…' : `Approve ${selectedIds.size}`}
              </button>
            )}
          </div>
        )}
        {experiments.map((exp) => {
          const spendCap = (exp.payloads?.[0] as Record<string, unknown> | undefined)?.spend_cap_usd;
          const capLabel = spendCap != null ? ` · $${Number(spendCap).toFixed(2)}` : '';
          const isBusy = busy || actionId === exp.id;
          const checked = selectedIds.has(exp.id);
          return (
            <div
              key={exp.id}
              className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 flex items-center justify-between gap-3"
              data-testid="held-experiment-row"
            >
              <div className="flex items-center gap-3 min-w-0 flex-1">
                {onApprove && (
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleSelect(exp.id)}
                    disabled={busy}
                    className="rounded border-neutral-600 bg-neutral-800 text-sky-500 focus:ring-sky-500 shrink-0 disabled:opacity-40"
                    data-testid="held-experiment-checkbox"
                  />
                )}
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-amber-200 break-words">{exp.title}{capLabel}</div>
                  <div className="text-ui-sm text-amber-300/70 font-mono">spend cap requires operator approval</div>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {onApprove && (
                  <button
                    type="button"
                    onClick={() => void handleApprove(exp.id)}
                    disabled={isBusy}
                    className="min-h-[44px] flex items-center gap-1.5 rounded bg-emerald-500/20 px-3 text-ui-md font-semibold text-emerald-200 hover:bg-emerald-500/30 transition-colors disabled:opacity-50"
                    data-testid="held-experiment-approve"
                  >
                    <CheckCircle className="w-3 h-3" />
                    {actionId === exp.id ? 'Approving…' : 'Approve'}
                  </button>
                )}
                {onDismiss && (
                  <button
                    type="button"
                    onClick={() => void handleDismiss(exp.id)}
                    disabled={isBusy}
                    className="min-h-[44px] flex items-center gap-1.5 rounded bg-red-500/20 px-3 text-ui-md font-semibold text-red-200 hover:bg-red-500/30 transition-colors disabled:opacity-50"
                    data-testid="held-experiment-dismiss"
                  >
                    <XCircle className="w-3 h-3" />
                    {actionId === exp.id ? 'Dismissing…' : 'Dismiss'}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function buildCloneForm(job: ComputeJob): ComputeExperimentForm {
  const lp = job.launch_params ?? {};
  const armsRaw = lp.arms;
  const useArmsMode = Array.isArray(armsRaw) && armsRaw.length > 0;
  const arms = useArmsMode
    ? (armsRaw as Array<{ name: string; command: string }>)
    : [{ name: '', command: '' }, { name: '', command: '' }];
  // env values are redacted server-side; env_keys carries only the key names
  const envVars = '';
  const sc = job.success_criterion;
  return {
    ...defaultComputeExperimentForm(),
    useArmsMode,
    arms,
    command: lp.command ?? '',
    declaredMetric: lp.declared_metric ?? '',
    metric: lp.metric ?? '',
    cwd: lp.cwd ?? '',
    envVars,
    accelerator: lp.accelerator ?? '',
    machineType: lp.machine_type ?? '',
    containerImage: lp.container_image ?? '',
    provisioningModel: lp.provisioning_model === 'SPOT' ? 'SPOT' : 'STANDARD',
    spendCapUsd: lp.spend_cap_usd != null ? String(lp.spend_cap_usd) : '',
    deadlineSeconds: lp.deadline_seconds != null ? String(lp.deadline_seconds) : '',
    requireLiveRelease: lp.require_live_release ?? false,
    requireDurableArtifact: lp.require_durable_artifact ?? false,
    experiment: job.experiment ?? '',
    benchmark: job.benchmark ?? '',
    hypothesis: job.hypothesis ?? '',
    successMetric: sc?.metric ?? '',
    successOperator: (sc?.operator as ComputeSuccessOperator) ?? '>=',
    successThreshold: sc?.threshold != null ? String(sc.threshold) : '',
    setupStep: lp.setup_step ?? '',
    noProgressTimeoutSeconds: lp.no_progress_timeout_seconds != null ? String(lp.no_progress_timeout_seconds) : '',
    inputArtifactJobId: lp.input_artifact_job_id ?? '',
  };
}

type TableSortKey = 'name' | 'status' | 'criterion' | 'cost' | 'runtime' | 'backend' | 'tier';

const TABLE_COL_LABELS: Record<TableSortKey, string> = {
  name: 'Name',
  status: 'Status',
  criterion: 'Criterion',
  cost: 'Cost',
  runtime: 'Runtime',
  backend: 'Backend',
  tier: 'Tier',
};

export function CompactJobsTable({
  jobs,
  sortConfig,
  onSort,
  onNavigateToTask,
}: {
  jobs: ComputeJob[];
  sortConfig: { key: TableSortKey; dir: 'asc' | 'desc' } | null;
  onSort: (key: TableSortKey) => void;
  onNavigateToTask?: (taskId: string) => void;
}) {
  return (
    <div className="overflow-x-auto" data-testid="compact-jobs-table">
      <table className="w-full text-ui-sm font-mono">
        <thead>
          <tr className="border-b border-neutral-800">
            {(Object.keys(TABLE_COL_LABELS) as TableSortKey[]).map((col) => (
              <th
                key={col}
                className="text-left py-1.5 pr-3 text-neutral-500 font-semibold cursor-pointer hover:text-neutral-300 select-none whitespace-nowrap"
                onClick={() => onSort(col)}
                data-testid={`jobs-table-sort-${col}`}
              >
                {TABLE_COL_LABELS[col]}
                {sortConfig?.key === col ? (sortConfig.dir === 'asc' ? ' ↑' : ' ↓') : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const active = isComputeJobActive(job);
            const spent = computeJobSpent(job);
            const runtimeSec = computeJobRuntimeSec(job);
            const { label: backendLabel } = computeJobExecutionLocation(job.backend ?? '');
            const runnerName = job.runner?.hostname ?? (job.runner_target_id ? job.runner_target_id.slice(0, 8) : null);
            const cv = job.criterion_verdict;
            const sc = job.success_criterion;
            return (
              <tr key={job.id} className="border-t border-neutral-800/50 hover:bg-neutral-800/30 transition-colors">
                <td className="py-1.5 pr-3 max-w-xs">
                  {job.task_id && onNavigateToTask ? (
                    <button
                      onClick={() => onNavigateToTask(job.task_id as string)}
                      className="text-sky-300 hover:underline text-left truncate max-w-full block"
                      data-testid="compact-table-job-name"
                    >
                      {(job.task_title ?? job.id).slice(0, 48)}
                    </button>
                  ) : (
                    <span className="text-neutral-300 truncate block" data-testid="compact-table-job-name">
                      {(job.task_title ?? job.id).slice(0, 48)}
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3 whitespace-nowrap">
                  <span className={`rounded border px-1.5 py-0.5 text-ui-xs font-semibold ${statusBadgeClass(job)}`}>
                    {active ? job.status : computeJobTerminalLabel(job)}
                  </span>
                </td>
                <td className="py-1.5 pr-3">
                  {sc ? (
                    <span className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-neutral-400 whitespace-nowrap">{sc.metric} {sc.operator} {sc.threshold}</span>
                      {cv && (
                        <span
                          className={`rounded border px-1 py-0.5 text-ui-xs font-semibold ${cv.met ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : !cv.measurable ? 'border-amber-500/40 bg-amber-500/15 text-amber-300' : 'border-red-500/40 bg-red-500/15 text-red-300'}`}
                          data-testid="compact-table-criterion-chip"
                        >
                          {cv.met ? 'met' : !cv.measurable ? 'inconclusive' : 'unmet'}
                        </span>
                      )}
                    </span>
                  ) : <span className="text-neutral-600">—</span>}
                </td>
                <td className="py-1.5 pr-3 text-neutral-400 whitespace-nowrap">
                  {spent != null ? fmtUsd(spent) : '—'}
                </td>
                <td className="py-1.5 pr-3 text-neutral-400 whitespace-nowrap">
                  {runtimeSec != null ? fmtDuration(runtimeSec) : active ? 'running' : '—'}
                </td>
                <td className="py-1.5 pr-3 text-neutral-400 max-w-xs">
                  <span className="flex items-center gap-1.5 flex-wrap min-w-0">
                    <span className="truncate">{backendLabel}</span>
                    {runnerName && (
                      <span
                        className="text-neutral-300 truncate max-w-[10rem]"
                        title={job.runner?.hostname ?? job.runner_target_id ?? undefined}
                        data-testid="compact-table-runner"
                      >
                        · {runnerName}
                      </span>
                    )}
                    {job.input_artifact_job_id && (
                      <span
                        className="text-emerald-500 shrink-0"
                        title={`Resumed from ${job.input_artifact_job_id}`}
                        data-testid="compact-table-resume"
                      >
                        ↻
                      </span>
                    )}
                  </span>
                </td>
                <td className="py-1.5 pr-3 whitespace-nowrap">
                  {!active ? <EvidenceTierBadge tier={computeJobEvidenceTier(job)} /> : <span className="text-neutral-600">—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface ComputeJobsPanelProps {
  projectId: string | null;
  onNavigateToTask?: (taskId: string) => void;
  onLaunchCompute?: () => void;
  onCloneExperiment?: (form: ComputeExperimentForm) => void;
  onDuplicateTask?: (taskId: string) => Promise<void>;
  onApproveExperiment?: (taskId: string) => Promise<void>;
  onDismissExperiment?: (taskId: string) => Promise<void>;
  onNavigateToKnowledge?: (documentId: string) => void;
}

export function ComputeJobsPanel({ projectId, onNavigateToTask, onLaunchCompute, onCloneExperiment, onApproveExperiment, onDismissExperiment, onNavigateToKnowledge }: ComputeJobsPanelProps) {
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const [heldExperiments, setHeldExperiments] = useState<HeldComputeExperiment[]>([]);
  const [awaitingHolds, setAwaitingHolds] = useState<AwaitingLocalRunnerHold[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [summary, setSummary] = useState<ComputeJobsSummary | null>(null);
  const [lifetimeSummary, setLifetimeSummary] = useState<ComputeJobsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [finishedWindowMinutes, setFinishedWindowMinutes] = useState(360);
  const [allTerminal, setAllTerminal] = useState(false);
  const [killingId, setKillingId] = useState<string | null>(null);
  const [pendingKill, setPendingKill] = useState<ComputeJob | null>(null);
  const [analyzingId, setAnalyzingId] = useState<string | null>(null);
  const [rerunState, setRerunState] = useState<{
    jobId: string;
    taskId: string;
    spendCap: string;
    deadlineSec: string;
    threshold: string;
    criterion: { metric: string; operator: ComputeSuccessOperator } | null;
    submitting: boolean;
    error: string | null;
    result?: { id: string; status?: string; requires_approval?: boolean };
  } | null>(null);
  const [analysisFeedback, setAnalysisFeedback] = useState<Record<string, { kind: 'running' | 'upgraded' | 'analysis_unavailable' | 'error'; message: string }>>({});
  const [analysisDocs, setAnalysisDocs] = useState<Record<string, KnowledgeDocument>>({});
  const [knowledgeDocs, setKnowledgeDocs] = useState<Record<string, KnowledgeDocument>>({});
  const [sweepGroups, setSweepGroups] = useState<ComputeSweepGroupsResponse['sweep_groups']>({});
  const [expandedDocIds, setExpandedDocIds] = useState<Set<string>>(new Set());
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [askState, setAskState] = usePersistedState<Record<string, AskStateValue>>(
    `compute-ask-state:${projectId}`,
    {},
    sanitizeAskState,
  );
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'completed' | 'failed'>('all');
  const [tierFilter, setTierFilter] = useState<'all' | 'machine-verified' | 'low-trust'>('all');
  const [viewMode, setViewMode] = useState<'card' | 'table'>('card');
  const [sortConfig, setSortConfig] = useState<{ key: TableSortKey; dir: 'asc' | 'desc' } | null>(null);

  const filteredJobs = useMemo(() => {
    let result = jobs;
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      result = result.filter((job) =>
        (job.task_title ?? job.id ?? '').toLowerCase().includes(q) ||
        (job.experiment ?? '').toLowerCase().includes(q) ||
        (job.benchmark ?? '').toLowerCase().includes(q) ||
        (job.hypothesis ?? '').toLowerCase().includes(q),
      );
    }
    if (statusFilter === 'active') {
      result = result.filter((job) => isComputeJobActive(job));
    } else if (statusFilter === 'completed') {
      result = result.filter((job) => !isComputeJobActive(job) && job.outcome === 'SUCCESS');
    } else if (statusFilter === 'failed') {
      result = result.filter((job) => !isComputeJobActive(job) && job.outcome !== null && job.outcome !== 'SUCCESS');
    }
    // Evidence tier only exists for TERMINAL jobs (active jobs render no badge and
    // return group `null`), so any non-`all` tier narrowing is inherently a filter
    // over finished results and drops active jobs.
    if (tierFilter !== 'all') {
      result = result.filter((job) => computeJobEvidenceGroup(job) === tierFilter);
    }
    return result;
  }, [jobs, searchText, statusFilter, tierFilter]);

  const displayJobs = useMemo(() => {
    if (!sortConfig) return filteredJobs;
    return [...filteredJobs].sort((a, b) => {
      let cmp = 0;
      if (sortConfig.key === 'name') cmp = (a.task_title ?? a.id).localeCompare(b.task_title ?? b.id);
      else if (sortConfig.key === 'status') cmp = (a.status ?? '').localeCompare(b.status ?? '');
      else if (sortConfig.key === 'criterion') {
        const am = a.criterion_verdict?.met ?? null;
        const bm = b.criterion_verdict?.met ?? null;
        cmp = am === bm ? 0 : am === true ? -1 : bm === true ? 1 : am === false ? -1 : 1;
      } else if (sortConfig.key === 'cost') cmp = (computeJobSpent(a) ?? 0) - (computeJobSpent(b) ?? 0);
      else if (sortConfig.key === 'runtime') cmp = (computeJobRuntimeSec(a) ?? 0) - (computeJobRuntimeSec(b) ?? 0);
      else if (sortConfig.key === 'backend') cmp = (a.backend ?? '').localeCompare(b.backend ?? '');
      else if (sortConfig.key === 'tier') cmp = computeJobEvidenceTierRank(a) - computeJobEvidenceTierRank(b);
      return sortConfig.dir === 'asc' ? cmp : -cmp;
    });
  }, [filteredJobs, sortConfig]);

  const handleTableSort = useCallback((key: TableSortKey) => {
    setSortConfig((prev) =>
      prev?.key === key ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' },
    );
  }, []);

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

  // Poll held (requires_approval) compute experiments every 30s so operators
  // see them here without visiting the Board.
  usePoller(
    async (signal) => {
      if (!projectId) return;
      try {
        const held = await fetchHeldComputeExperiments(projectId, signal);
        setHeldExperiments(Array.isArray(held) ? held : []);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        // Non-fatal: held section is supplementary; don't surface as panel error
      }
    },
    { intervalMs: 30_000, enabled: !!projectId, deps: [projectId] },
  );

  // Poll compute_experiment tasks held QUEUED awaiting a local runner every 30s.
  // These have no ComputeJob row, so this endpoint is the only surface for them.
  usePoller(
    async (signal) => {
      if (!projectId) return;
      try {
        const holds = await fetchAwaitingLocalRunnerHolds(projectId, signal);
        setAwaitingHolds(Array.isArray(holds) ? holds : []);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        // Non-fatal: awaiting-local-runner section is supplementary
      }
    },
    { intervalMs: 30_000, enabled: !!projectId, deps: [projectId] },
  );

  // Poll sweep-grouped knowledge docs every 60s — complementary to the live job
  // poll, uses durable knowledge documents so changes are infrequent.
  usePoller(
    async (signal) => {
      if (!projectId) return;
      try {
        const result = await fetchComputeSweepGroups(projectId, signal);
        setSweepGroups(result.sweep_groups ?? {});
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        // Non-fatal: sweep groups are supplementary
      }
    },
    { intervalMs: 60_000, enabled: !!projectId, deps: [projectId] },
  );

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

  // Local 1s clock drives the live deadline countdowns between polls, plus the
  // "waiting for local runner since … ago" indicator for awaiting-runner holds.
  const hasActive = jobs.some(isComputeJobActive);
  const needsClock = hasActive || awaitingHolds.length > 0;
  useEffect(() => {
    if (!needsClock) return;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [needsClock]);

  const handleKill = useCallback(
    (job: ComputeJob) => {
      setPendingKill(job);
    },
    [],
  );

  const executeKill = useCallback(
    async (job: ComputeJob) => {
      setPendingKill(null);
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

  const handleRerunOpen = useCallback((job: ComputeJob) => {
    if (!job.task_id) return;
    setRerunState({
      jobId: job.id,
      taskId: job.task_id,
      spendCap: job.spend_cap_usd != null ? String(job.spend_cap_usd) : '',
      deadlineSec: job.launch_contract?.requested_deadline_seconds != null
        ? String(job.launch_contract.requested_deadline_seconds)
        : '',
      threshold: job.success_criterion?.threshold != null ? String(job.success_criterion.threshold) : '',
      criterion: job.success_criterion
        ? { metric: job.success_criterion.metric, operator: job.success_criterion.operator as ComputeSuccessOperator }
        : null,
      submitting: false,
      error: null,
    });
  }, []);

  const handleRerunSubmit = useCallback(async () => {
    if (!rerunState) return;
    setRerunState((prev) => prev ? { ...prev, submitting: true, error: null } : prev);
    try {
      const body: { success_criterion?: { metric: string; operator: ComputeSuccessOperator; threshold: number }; spend_cap_usd?: number; deadline_seconds?: number } = {};
      if (rerunState.spendCap !== '') body.spend_cap_usd = parseFloat(rerunState.spendCap);
      if (rerunState.deadlineSec !== '') body.deadline_seconds = parseInt(rerunState.deadlineSec, 10);
      if (rerunState.criterion && rerunState.threshold !== '') {
        body.success_criterion = {
          metric: rerunState.criterion.metric,
          operator: rerunState.criterion.operator,
          threshold: parseFloat(rerunState.threshold),
        };
      }
      const response = await rerunComputeExperiment(rerunState.taskId, body);
      setRerunState((prev) => prev ? { ...prev, submitting: false, result: response } : prev);
      await load();
    } catch (e) {
      setRerunState((prev) => prev ? { ...prev, submitting: false, error: e instanceof Error ? e.message : 'Re-run failed' } : prev);
    }
  }, [rerunState, load]);

  const handleAsk = useCallback(async (jobId: string) => {
    const q = (askState[jobId]?.question ?? '').trim();
    if (!q) return;
    const prevThread = askState[jobId]?.thread ?? [];
    const history = prevThread.map((t) => ({ question: t.question, answer: t.result.answer ?? '' }));
    setAskState((prev) => ({
      ...prev,
      [jobId]: { ...(prev[jobId] ?? { question: q, submitting: false, thread: [], error: null }), submitting: true, error: null },
    }));
    try {
      const result = await askComputeJob(jobId, q, history.length > 0 ? history : undefined);
      setAskState((prev) => {
        const cur = prev[jobId] ?? { question: q, submitting: false, thread: [], error: null };
        return {
          ...prev,
          [jobId]: { ...cur, submitting: false, question: '', thread: [...(cur.thread ?? []), { question: q, result }] },
        };
      });
    } catch (e) {
      setAskState((prev) => ({
        ...prev,
        [jobId]: { ...(prev[jobId] ?? { question: q, submitting: false, thread: [], error: null }), submitting: false, error: e instanceof Error ? e.message : 'Ask failed' },
      }));
    }
  }, [askState, setAskState]);

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
      <div className="flex flex-wrap items-center justify-between gap-2 mb-1">
        <div className="flex items-center gap-2 text-ui-md font-bold text-neutral-400 uppercase tracking-widest">
          <Cpu className="w-3.5 h-3.5 shrink-0" />
          Compute jobs
        </div>
        {/* flex-wrap + min-w-0: window selector (6×44px touch targets) plus the
            view-mode toggle exceed a 375px viewport on one line — let the
            controls wrap under the title instead of overflowing horizontally. */}
        <div className="flex flex-wrap items-center justify-end gap-2 min-w-0">
          {onLaunchCompute && (
            <button
              type="button"
              onClick={onLaunchCompute}
              className="shrink-0 min-h-[36px] rounded bg-sky-500/20 px-3 text-ui-sm font-semibold text-sky-200 transition-colors hover:bg-sky-500/30"
            >
              Launch experiment
            </button>
          )}
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
          <button
            type="button"
            onClick={() => setViewMode((m) => m === 'card' ? 'table' : 'card')}
            className={`min-h-[44px] min-w-[44px] flex items-center justify-center rounded px-1.5 py-0.5 transition-colors ${viewMode === 'table' ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300'}`}
            title={viewMode === 'table' ? 'Switch to card view' : 'Switch to compact table view'}
            data-testid="compute-view-mode-toggle"
          >
            <List className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
      {!allTerminal && (
        <div className="mb-2 text-ui-xs text-neutral-500" data-testid="compute-window-indicator">
          Showing jobs from the last {WINDOW_OPTIONS.find(o => o.minutes === finishedWindowMinutes)?.label || '6h'}
        </div>
      )}

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
        <div className="mb-3 flex flex-col gap-2" data-testid="compute-jobs-summary">
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-ui-sm font-mono text-neutral-400">
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
          {Object.keys(summary.outcome_breakdown).length > 0 && (
            <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-ui-sm font-mono text-neutral-400">
              <span className="text-neutral-500 text-xs uppercase tracking-widest">Outcomes</span>
              {Object.entries(summary.outcome_breakdown).map(([outcome, count]) => (
                <span key={outcome}>
                  <span className="text-neutral-500">{outcome}</span> {count}
                </span>
              ))}
            </div>
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
          <div className="text-xs font-semibold text-neutral-200">No compute jobs</div>
          <div className="mt-0.5 text-ui-sm text-neutral-500">
            Launch a structured experiment for this project.
          </div>
        </div>
      )}

      <AwaitingLocalRunnerSection holds={awaitingHolds} nowMs={nowMs} />

      <HeldExperimentsSection
        experiments={heldExperiments}
        onApprove={onApproveExperiment}
        onDismiss={onDismissExperiment}
      />

      <SweepKnowledgeGroupsSection sweepGroups={sweepGroups} onNavigateToKnowledge={onNavigateToKnowledge} />

      <ExperimentSweepSummary jobs={jobs} />

      {(jobs.length > 0 || searchText || statusFilter !== 'all' || tierFilter !== 'all') && (
        <div className="mb-2 flex flex-wrap items-center gap-2" data-testid="compute-jobs-filters">
          <div className="relative flex-1 min-w-0 max-w-xs">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-500 pointer-events-none" />
            <input
              type="text"
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              placeholder="Search by name, experiment…"
              className="w-full rounded border border-neutral-700 bg-neutral-900 pl-7 pr-2 py-1.5 text-ui-sm text-neutral-100 placeholder-neutral-600 focus:outline-none focus:border-sky-500/50"
              data-testid="compute-jobs-search"
            />
          </div>
          <div className="flex items-center gap-0.5" data-testid="compute-status-filter">
            {(['all', 'active', 'completed', 'failed'] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setStatusFilter(s)}
                className={`min-h-[44px] min-w-[44px] rounded px-1.5 py-0.5 text-ui-xs font-semibold transition-colors ${statusFilter === s ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300'}`}
                data-testid={`compute-status-${s}`}
              >
                {s === 'all' ? 'All' : s === 'active' ? 'Active' : s === 'completed' ? 'Completed' : 'Failed'}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-0.5" data-testid="compute-tier-filter">
            {(['all', 'machine-verified', 'low-trust'] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTierFilter(t)}
                className={`min-h-[44px] min-w-[44px] rounded px-1.5 py-0.5 text-ui-xs font-semibold transition-colors ${tierFilter === t ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300'}`}
                data-testid={`compute-tier-${t}`}
                title={
                  t === 'all'
                    ? 'Show all evidence tiers'
                    : t === 'machine-verified'
                      ? 'Only machine-verified results (executed and verified on operator hardware)'
                      : 'Only low-trust results (attested / backfilled / partial / self-reported)'
                }
              >
                {t === 'all' ? 'All tiers' : t === 'machine-verified' ? 'Machine verified' : 'Low trust'}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2" data-testid="compute-jobs-list">
        {filteredJobs.length === 0 && jobs.length > 0 && (
          <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/40 px-3 py-2">
            <div className="text-ui-sm text-neutral-500">No jobs match the current filters.</div>
          </div>
        )}
        {viewMode === 'table' && filteredJobs.length > 0 && (
          <CompactJobsTable
            jobs={displayJobs}
            sortConfig={sortConfig}
            onSort={handleTableSort}
            onNavigateToTask={onNavigateToTask}
          />
        )}
        {viewMode !== 'table' && displayJobs.map((job) => {
          const active = isComputeJobActive(job);
          const cap = job.spend_cap_usd ?? 0;
          const spent = computeJobSpent(job);
          const pct = cap > 0 ? Math.min(1, spent / cap) : 0;
          const { label: backendLabel, isOperatorHardware } = computeJobExecutionLocation(job.backend ?? '');
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
                  <div
                    className="text-ui-sm text-neutral-500 font-mono mt-0.5 break-words"
                    data-testid="compute-job-provenance"
                  >
                    {backendLabel}
                    {job.machine_type ? ` · ${job.machine_type}` : ''}
                    {job.accelerator_type ? ` · ${job.accelerator_type} x${job.accelerator_count ?? 1}` : ''}
                    {job.launch_contract?.provisioning_model ? ` · ${job.launch_contract.provisioning_model}` : ''}
                    {job.external_compute_id ? (
                      <> · <span data-testid="compute-job-vm-identity">VM {job.external_compute_id}</span></>
                    ) : ''}
                  </div>
                  {(() => {
                    const runner = job.runner;
                    const runnerName = runner?.hostname ?? (job.runner_target_id ? job.runner_target_id.slice(0, 8) : null);
                    if (runnerName) {
                      const claimedMs = job.runner_claimed_at ? Date.parse(job.runner_claimed_at) : NaN;
                      const elapsedSec = isNaN(claimedMs) ? null : Math.max(0, Math.round((nowMs - claimedMs) / 1000));
                      return (
                        <div className="text-ui-sm text-neutral-400 font-mono mt-0.5" data-testid="compute-runner-claimed">
                          runner: <span className="text-neutral-200">{runnerName}</span>
                          {elapsedSec != null && (
                            <> · claimed {fmtDuration(elapsedSec)} ago</>
                          )}
                        </div>
                      );
                    }
                    if (job.awaiting_local_runner_since && !runner) {
                      const waitingMs = Date.parse(job.awaiting_local_runner_since);
                      const waitingSec = isNaN(waitingMs) ? null : Math.max(0, Math.round((nowMs - waitingMs) / 1000));
                      return (
                        <div className="text-ui-sm text-amber-400 font-mono mt-0.5" data-testid="compute-runner-waiting">
                          waiting for local runner
                          {waitingSec != null && <> since {fmtDuration(waitingSec)} ago</>}
                        </div>
                      );
                    }
                    return null;
                  })()}
                  {(() => {
                    const inputJobId = job.input_artifact_job_id;
                    if (!inputJobId) return null;
                    const truncated = inputJobId.length > 8 ? inputJobId.slice(0, 8) + '…' : inputJobId;
                    const prov = job.resume_provenance;
                    const ok = prov?.ok === true;
                    const reason = typeof prov?.reason === 'string' ? prov.reason : null;
                    const fileCount = typeof prov?.file_count === 'number' ? prov.file_count : null;
                    const resumedFrom = typeof prov?.resumed_from_job_id === 'string' ? prov.resumed_from_job_id : null;
                    const displayId = resumedFrom ?? truncated;
                    return (
                      <div
                        className={`text-ui-sm font-mono mt-0.5 ${ok ? 'text-emerald-400' : reason ? 'text-amber-400' : 'text-sky-400'}`}
                        data-testid="compute-resume-provenance"
                      >
                        resumed from{' '}
                        <span
                          className={ok ? 'text-emerald-200' : reason ? 'text-amber-200' : 'text-sky-200'}
                        >
                          {displayId}
                        </span>
                        {ok && fileCount != null && <> · {fileCount} files</>}
                        {reason && <> · {reason}</>}
                      </div>
                    );
                  })()}
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
                <div className="flex shrink-0 items-center gap-1.5">
                  {computeJobImagePinStatus(job) === 'mutable' && <MutableImageBadge />}
                  {!active && (
                    <EvidenceTierBadge tier={computeJobEvidenceTier(job)} />
                  )}
                  <span
                    className={`shrink-0 rounded border px-1.5 py-0.5 text-ui-sm font-semibold ${statusBadgeClass(job)}`}
                  >
                    {active ? job.status : computeJobTerminalLabel(job)}
                  </span>
                </div>
              </div>

              {isOperatorHardware ? (
                <div className="mb-1.5 flex items-center justify-between text-ui-sm text-neutral-400 font-mono" data-testid="compute-operator-cost">
                  <span>Cost</span>
                  <span>$0.00 · not billed at cloud rates</span>
                </div>
              ) : (
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
              )}

              {(() => {
                const { experiment, benchmark, hypothesis } = job;
                const hasIdentity = !!(experiment || benchmark || hypothesis);
                if (!hasIdentity) return null;
                return (
                  <div className="mb-1.5 rounded border border-neutral-700/50 bg-neutral-800/40 px-2 py-1.5 flex flex-col gap-0.5">
                    <div
                      className="flex flex-col gap-0.5 text-ui-sm font-mono"
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
                      {job.success_criterion && (
                        <div className="flex items-start gap-1.5">
                          <span className="text-neutral-500 shrink-0">Criterion</span>
                          <span className="text-neutral-300 break-words">
                            {job.success_criterion.metric} {job.success_criterion.operator} {job.success_criterion.threshold}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })()}

              {(() => {
                const prov = job.launch_params?.provenance;
                const hasProvenance = !!(prov?.idea || prov?.draft_notes);
                if (!hasProvenance) return null;
                return (
                  <div
                    className="mb-1.5 rounded border border-neutral-700/50 bg-neutral-800/40 px-2 py-1.5 flex flex-col gap-0.5"
                    data-testid="compute-draft-provenance"
                  >
                    {prov?.drafted_from_refinement && (
                      <div className="flex items-center gap-1.5 text-ui-sm font-mono text-neutral-500 mb-0.5">
                        <span>From AI draft</span>
                      </div>
                    )}
                    {prov?.idea && (
                      <div className="flex items-start gap-1.5 text-ui-sm font-mono">
                        <span className="text-neutral-500 shrink-0">Idea</span>
                        <span className="text-neutral-300 break-words">{prov.idea}</span>
                      </div>
                    )}
                    {prov?.draft_notes && (
                      <div className="flex items-start gap-1.5 text-ui-sm font-mono">
                        <span className="text-neutral-500 shrink-0">Draft notes</span>
                        <span className="text-neutral-400 break-words">{prov.draft_notes}</span>
                      </div>
                    )}
                  </div>
                );
              })()}

              {(() => {
                const rawCommand = job.launch_params?.command;
                if (!rawCommand) return null;
                const decoded = job.launch_params?.command_decoded;
                return (
                  <details
                    className="mb-1.5 rounded border border-neutral-700/50 bg-neutral-800/40 px-2 py-1.5"
                    data-testid="compute-command-disclosure"
                  >
                    <summary className="cursor-pointer text-ui-sm font-mono text-neutral-500 hover:text-neutral-300">
                      Command{decoded ? ' (decoded)' : ''}
                    </summary>
                    <pre
                      className="mt-1.5 overflow-x-auto whitespace-pre-wrap break-words text-ui-sm font-mono text-neutral-300"
                      data-testid="compute-command-text"
                    >
                      {decoded ?? rawCommand}
                    </pre>
                  </details>
                );
              })()}

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
                if (!active) return null;
                // Older backends only send watchdog_active. Preserve that display
                // while preferring the additive, honest state when available.
                if (job.watchdog_state == null && job.watchdog_active == null) return null;
                const timeoutSec = job.no_progress_timeout_seconds;
                const lastProgressAt = job.last_progress_at;
                const inertReason = job.watchdog_inert_reason;
                const watchdogState = job.watchdog_state
                  ?? (job.watchdog_active ? 'protected' : 'legacy_unprotected');
                const protectedNow = watchdogState === 'protected';
                const stateLabel = watchdogState === 'warming_up'
                  ? 'Warming up'
                  : watchdogState === 'insufficient_history'
                    ? 'Insufficient history'
                    : watchdogState === 'stale_telemetry'
                      ? 'Stale telemetry'
                      : watchdogState === 'inert'
                        ? 'Inert'
                        : 'Unprotected';
                const stateExplanation = watchdogState === 'warming_up'
                  ? 'Watchdog will not currently cut this job: its observation window is still warming up.'
                  : watchdogState === 'insufficient_history'
                    ? 'Watchdog will not currently cut this job: there is not enough trustworthy receipt-time history.'
                    : watchdogState === 'stale_telemetry'
                      ? 'Watchdog will not currently cut this job: progress telemetry is stale.'
                      : watchdogState === 'inert'
                        ? `Watchdog will not currently cut this job: ${inertReason ?? 'not_configured'}. A stalled run may continue until its wall-clock deadline.`
                        : inertReason
                          ? `Watchdog will not currently cut this job: ${inertReason}. A stalled run may continue until its wall-clock deadline.`
                          : null;
                let lastProgressAgo: string | null = null;
                if (lastProgressAt) {
                  const diffSec = Math.max(0, Math.round((nowMs - new Date(lastProgressAt).getTime()) / 1000));
                  lastProgressAgo = fmtDuration(diffSec) + ' ago';
                }
                return (
                  <div className="mb-1.5" data-testid="compute-watchdog-status">
                    {protectedNow ? (
                      <div className="flex flex-wrap items-center gap-1.5 text-ui-sm">
                        <span
                          className="rounded border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-ui-xs font-semibold text-emerald-300"
                          data-testid="compute-watchdog-protected"
                        >
                          Protected
                        </span>
                        {timeoutSec != null && (
                          <span className="text-neutral-500 font-mono">
                            cut at {fmtDuration(timeoutSec)} no-progress
                          </span>
                        )}
                        {lastProgressAgo && (
                          <span className="text-neutral-500 font-mono">
                            · last progress {lastProgressAgo}
                          </span>
                        )}
                      </div>
                    ) : (
                      <>
                        <div className="flex flex-wrap items-center gap-1.5 text-ui-sm">
                          <span
                            className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-ui-xs font-semibold text-amber-300"
                            data-testid="compute-watchdog-unprotected"
                          >
                            {stateLabel}
                          </span>
                          {lastProgressAgo && (
                            <span className="text-neutral-500 font-mono">
                              · last progress {lastProgressAgo}
                            </span>
                          )}
                        </div>
                        {stateExplanation && (
                          <div
                            className="mt-1 rounded border border-amber-500/30 bg-amber-500/8 px-2 py-1.5 text-ui-sm text-amber-200"
                            data-testid="compute-watchdog-inert-warning"
                          >
                            <div className="flex items-start gap-1.5">
                              <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                              <span>{stateExplanation}</span>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
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
                  {!active && typeof job.exit_code === 'number' && (
                    <span
                      className="shrink-0 rounded border border-neutral-700 bg-neutral-800/60 px-1 py-0.5 text-ui-xs text-neutral-400"
                      data-testid="compute-exit-code"
                    >
                      exit {job.exit_code}
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
                      {onCloneExperiment && (
                        <button
                          onClick={() => onCloneExperiment(buildCloneForm(job))}
                          disabled={!job.launch_params}
                          className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30 text-ui-md font-semibold px-3 transition-colors disabled:opacity-50"
                          title="Clone this experiment's configuration into the launch form"
                          data-testid="clone-to-form"
                        >
                          Clone to form
                        </button>
                      )}
                      {rerunState?.jobId === job.id ? (
                        <div className="flex flex-col gap-2 rounded border border-sky-500/30 bg-sky-500/10 p-2 text-ui-sm" data-testid="rerun-inline-panel">
                          {rerunState.result ? (
                            <>
                              <div className="font-semibold text-emerald-200">Re-run launched</div>
                              <div className="flex flex-col gap-1.5">
                                <div>
                                  <span className="text-neutral-400">New task ID:</span>{' '}
                                  <span className="text-emerald-300 font-mono">{rerunState.result.id}</span>
                                </div>
                                {rerunState.result.requires_approval && (
                                  <div className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1">
                                    <span className="text-amber-300">Held for approval</span>
                                  </div>
                                )}
                              </div>
                              <button
                                onClick={() => setRerunState(null)}
                                className="rounded bg-neutral-700/40 text-neutral-300 hover:bg-neutral-700/60 px-3 py-1.5 text-ui-sm w-fit"
                                data-testid="rerun-result-close"
                              >
                                Close
                              </button>
                            </>
                          ) : (
                            <>
                              <div className="font-semibold text-sky-200">Re-run with adjustments</div>
                              <div className="flex flex-wrap gap-2">
                                <label className="flex flex-col gap-0.5">
                                  <span className="text-neutral-400">Spend cap (USD)</span>
                                  <input
                                    type="number"
                                    min="0.01"
                                    step="0.01"
                                    value={rerunState.spendCap}
                                    onChange={(e) => setRerunState((p) => p ? { ...p, spendCap: e.target.value } : p)}
                                    className="w-24 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-100 text-ui-sm"
                                    data-testid="rerun-spend-cap"
                                  />
                                </label>
                                <label className="flex flex-col gap-0.5">
                                  <span className="text-neutral-400">Deadline (sec)</span>
                                  <input
                                    type="number"
                                    min="1"
                                    step="1"
                                    value={rerunState.deadlineSec}
                                    onChange={(e) => setRerunState((p) => p ? { ...p, deadlineSec: e.target.value } : p)}
                                    className="w-24 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-100 text-ui-sm"
                                    data-testid="rerun-deadline-sec"
                                  />
                                </label>
                                {rerunState.criterion && (
                                  <label className="flex flex-col gap-0.5">
                                    <span className="text-neutral-400">Threshold ({rerunState.criterion.metric} {rerunState.criterion.operator})</span>
                                    <input
                                      type="number"
                                      step="any"
                                      value={rerunState.threshold}
                                      onChange={(e) => setRerunState((p) => p ? { ...p, threshold: e.target.value } : p)}
                                      className="w-24 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-100 text-ui-sm"
                                      data-testid="rerun-threshold"
                                    />
                                  </label>
                                )}
                              </div>
                              {rerunState.error && (
                                <div className="text-red-300 text-ui-xs" data-testid="rerun-error">{rerunState.error}</div>
                              )}
                              <div className="flex gap-2">
                                <button
                                  onClick={() => void handleRerunSubmit()}
                                  disabled={rerunState.submitting}
                                  className="rounded bg-sky-500/30 text-sky-200 hover:bg-sky-500/40 px-3 py-1.5 text-ui-sm font-semibold disabled:opacity-50"
                                  data-testid="rerun-submit"
                                >
                                  {rerunState.submitting ? 'Launching…' : 'Launch re-run'}
                                </button>
                                <button
                                  onClick={() => setRerunState(null)}
                                  className="rounded bg-neutral-700/40 text-neutral-300 hover:bg-neutral-700/60 px-3 py-1.5 text-ui-sm"
                                  data-testid="rerun-cancel"
                                >
                                  Cancel
                                </button>
                              </div>
                            </>
                          )}
                        </div>
                      ) : (
                        <button
                          onClick={() => handleRerunOpen(job)}
                          disabled={!job.task_id}
                          className="min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-sky-500/20 text-sky-200 hover:bg-sky-500/30 text-ui-md font-semibold px-3 transition-colors disabled:opacity-50"
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
                        <div className="flex items-center justify-between gap-2">
                          <button
                            type="button"
                            onClick={() => toggleDocExpanded(docKey)}
                            className="font-semibold text-sky-200 break-words text-left flex-1 hover:text-sky-100"
                            data-testid="compute-knowledge-doc-toggle"
                          >
                            {doc.title}
                          </button>
                          {onNavigateToKnowledge && (
                            <button
                              type="button"
                              onClick={() => onNavigateToKnowledge(doc.id)}
                              className="min-h-[44px] shrink-0 flex items-center gap-1 rounded px-1.5 py-0.5 text-ui-xs font-semibold text-sky-400 hover:text-sky-200 hover:bg-sky-500/15 transition-colors"
                              title="View in Knowledge library"
                              data-testid="compute-knowledge-doc-library-link"
                            >
                              <BookOpen className="w-3 h-3" />
                              Library
                            </button>
                          )}
                        </div>
                        <div className="mt-1 flex items-center gap-2 flex-wrap">
                          <AcceptanceGradeBadge doc={doc} />
                        </div>
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
              <ComputeArtifactGallery projectId={projectId} job={job} testIdPrefix="compute" />
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
                            const pav = job.per_arm_verdicts?.[arm.name];
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
                                  {pav != null && pav.met !== null && (
                                    <span
                                      className={`rounded border px-1 py-0.5 text-ui-xs font-semibold ${pav.met ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : 'border-red-500/40 bg-red-500/15 text-red-300'}`}
                                      data-testid="arm-verdict-chip"
                                    >
                                      {pav.met ? 'pass' : 'fail'}
                                    </span>
                                  )}
                                  {pav != null && pav.met === null && (
                                    <span
                                      className="rounded border px-1 py-0.5 text-ui-xs font-semibold border-amber-500/40 bg-amber-500/15 text-amber-300"
                                      data-testid="arm-verdict-chip"
                                    >
                                      n/a
                                    </span>
                                  )}
                                  {pav?.cost_usd != null && (
                                    <span className="text-ui-xs text-neutral-400 font-mono" data-testid="arm-cost">{fmtUsd(pav.cost_usd)}</span>
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
                const { hypothesis } = job;
                const sc = job.success_criterion;
                const verdict = job.criterion_verdict;
                if (!sc) return null;

                // Derive top-level verdict state when hypothesis + criterion are both present.
                type VerdictState = 'held' | 'refuted' | 'inconclusive';
                let verdictState: VerdictState | null = null;
                if (hypothesis && sc && verdict) {
                  verdictState = !verdict.measurable ? 'inconclusive' : verdict.met ? 'held' : 'refuted';
                }

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
                } else if (!verdict.measurable) {
                  chip = (
                    <span
                      className="rounded border border-amber-500/40 bg-amber-500/15 px-1 py-0.5 text-amber-300"
                      data-testid="criterion-inconclusive"
                    >
                      inconclusive
                    </span>
                  );
                } else {
                  chip = (
                    <span
                      className="rounded border border-red-500/40 bg-red-500/15 px-1 py-0.5 text-red-300"
                      data-testid="criterion-unmet"
                    >
                      unmet
                    </span>
                  );
                }

                return (
                  <div
                    className="mt-1.5"
                    data-testid={verdictState ? 'fused-experiment-verdict' : undefined}
                  >
                    {verdictState && (
                      <div className="mb-1 flex items-center gap-1.5">
                        <span
                          className={`rounded border px-1.5 py-0.5 text-ui-xs font-bold ${
                            verdictState === 'held'
                              ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                              : verdictState === 'refuted'
                                ? 'border-red-500/40 bg-red-500/15 text-red-300'
                                : 'border-amber-500/40 bg-amber-500/15 text-amber-300'
                          }`}
                          data-testid="verdict-label"
                        >
                          {verdictState === 'held' ? 'HELD' : verdictState === 'refuted' ? 'REFUTED' : 'INCONCLUSIVE'}
                        </span>
                      </div>
                    )}
                    <div
                      className="rounded border border-neutral-700/50 bg-neutral-800/40 px-2 py-1.5 flex flex-col gap-1 text-ui-sm font-mono"
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
                      {verdict?.binding_status && (
                        <div className="flex items-center gap-1.5 text-neutral-500">
                          <span className="shrink-0">Binding status</span>
                          <span
                            className="font-semibold text-neutral-300"
                            data-testid="criterion-binding-status"
                          >
                            {verdict.binding_status}
                          </span>
                        </div>
                      )}
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
                  </div>
                );
              })()}
              {(() => {
                if (!active) return null;
                const lo = job.polling_metadata?.live_output as ComputeJobLiveOutput | undefined | null;
                if (!lo || typeof lo.text !== 'string' || !lo.text.trim()) return null;
                const lines = lo.text.split('\n');
                const clipped = lines.length > 30;
                const liveKey = `liveout-${job.id}`;
                const liveExpanded = expandedDocIds.has(liveKey);
                const displayLines = clipped && !liveExpanded ? lines.slice(-30) : lines;
                return (
                  <div className="mt-1.5" data-testid="compute-live-output">
                    <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
                      <span>Live output</span>
                      <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-neutral-500">
                        updating…
                      </span>
                      {(lo.truncated || (clipped && !liveExpanded)) && (
                        <span className="rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-neutral-400">
                          truncated
                        </span>
                      )}
                      {clipped && !liveExpanded && (
                        <button
                          type="button"
                          onClick={() => toggleDocExpanded(liveKey)}
                          className="min-h-[44px] rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-sky-400 hover:text-sky-300"
                          data-testid="compute-live-output-show-all"
                        >
                          Show all ({lines.length} lines)
                        </button>
                      )}
                    </div>
                    <pre className="rounded border border-neutral-800 bg-neutral-950 px-2 py-1.5 text-ui-xs text-neutral-300 font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
                      {displayLines.join('\n')}
                    </pre>
                  </div>
                );
              })()}
              {(() => {
                const ps = job.progress_series;
                if (!ps || ps.length === 0) return null;
                const latest = ps[ps.length - 1];
                const name = latest.name;
                // Collect values for the same metric name (most common case), falling back to all values
                const seriesForMetric = ps.filter((s) => s.name === name);
                const vals = seriesForMetric.map((s) => s.value);
                const minVal = Math.min(...vals);
                const maxVal = Math.max(...vals);
                const range = maxVal - minVal || 1;
                const W = 80;
                const H = 20;
                const points = vals
                  .map((v, i) => {
                    const x = vals.length === 1 ? W / 2 : (i / (vals.length - 1)) * W;
                    const y = H - ((v - minVal) / range) * H;
                    return `${x.toFixed(1)},${y.toFixed(1)}`;
                  })
                  .join(' ');
                const isStalled = !active && job.terminal_kind === 'no_progress';
                return (
                  <div className="mt-1.5" data-testid="compute-progress-series">
                    <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
                      <span>{name}</span>
                      <span className="text-neutral-300">{latest.value}</span>
                      <span className="text-neutral-600">({ps.length} samples)</span>
                      {isStalled && (
                        <span className="rounded border border-amber-800 bg-amber-950 px-1 py-0.5 text-ui-xs text-amber-400">
                          no-progress cut
                        </span>
                      )}
                    </div>
                    <svg
                      width={W}
                      height={H}
                      viewBox={`0 0 ${W} ${H}`}
                      className="overflow-visible"
                      aria-hidden="true"
                    >
                      <polyline
                        points={points}
                        fill="none"
                        stroke={isStalled ? '#92400e' : '#38bdf8'}
                        strokeWidth="1.5"
                        strokeLinejoin="round"
                        strokeLinecap="round"
                      />
                    </svg>
                  </div>
                );
              })()}
              {(() => {
                if (active) return null;
                const cl = job.command_log ?? (job.polling_metadata?.command_log as ComputeJobCommandLog | undefined | null);
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
              {!active && (() => {
                const ask = askState[job.id] ?? { question: '', submitting: false, thread: [], error: null };

                // Suggest starter questions based on job content
                const starterQuestions: string[] = [];
                if (job.criterion_verdict) {
                  const cv = job.criterion_verdict;
                  const status = cv.met ? 'pass' : 'fail';
                  starterQuestions.push(`Why did this ${status} the ${cv.metric} criterion?`);
                  if (cv.value !== null && cv.threshold !== null) {
                    starterQuestions.push(`How did the measured ${cv.metric} (${cv.value}) compare to the threshold (${cv.threshold})?`);
                  }
                }
                const metrics = job.polling_metadata?.result_metrics as Record<string, unknown> | undefined;
                const arms = metrics ? normalizeArms(metrics.arms) : null;
                if (arms && arms.length > 1) {
                  starterQuestions.push(`Which arm performed best and why?`);
                  starterQuestions.push(`What were the per-arm results?`);
                }
                if (job.final_cost_usd !== null) {
                  starterQuestions.push(`Why did this run cost $${job.final_cost_usd.toFixed(2)}?`);
                }
                if (!starterQuestions.length && job.status !== 'SUCCEEDED') {
                  starterQuestions.push(`What went wrong in this run?`);
                }

                // Helper to parse citations into readable format
                const formatCitation = (c: string): { label: string; value: string } => {
                  // Citations are typically "source:identifier" format
                  // e.g., "result_metrics:accuracy" or "arm:control"
                  if (c.includes('://')) {
                    return { label: 'evidence', value: c };
                  }
                  const splitAt = c.indexOf(':');
                  if (splitAt > 0) {
                    return {
                      label: c.slice(0, splitAt),
                      value: c.slice(splitAt + 1),
                    };
                  }
                  return { label: 'evidence', value: c };
                };

                return (
                  <div className="mt-1.5" data-testid="compute-ask-section">
                    <form
                      onSubmit={(e) => { e.preventDefault(); void handleAsk(job.id); }}
                      className="flex flex-col gap-1.5"
                    >
                      <div className="flex gap-1.5">
                        <input
                          type="text"
                          value={ask.question}
                          onChange={(e) => setAskState((prev) => ({
                            ...prev,
                            [job.id]: { ...(prev[job.id] ?? { question: '', submitting: false, thread: [], error: null }), question: e.target.value },
                          }))}
                          placeholder={starterQuestions.length > 0 ? starterQuestions[0] : "Ask about this run…"}
                          disabled={ask.submitting}
                          className="flex-1 min-w-0 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-ui-sm text-neutral-100 placeholder-neutral-600"
                          data-testid="compute-ask-input"
                        />
                        <button
                          type="submit"
                          disabled={ask.submitting || !ask.question.trim()}
                          className="min-h-[44px] min-w-[44px] rounded bg-indigo-500/20 px-3 text-ui-sm font-semibold text-indigo-200 hover:bg-indigo-500/30 transition-colors disabled:opacity-50"
                          data-testid="compute-ask-submit"
                        >
                          {ask.submitting ? 'Asking…' : 'Ask'}
                        </button>
                      </div>
                      {!ask.question && starterQuestions.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {starterQuestions.slice(0, 2).map((q) => (
                            <button
                              key={q}
                              type="button"
                              onClick={() => setAskState((prev) => ({
                                ...prev,
                                [job.id]: { ...(prev[job.id] ?? { question: '', submitting: false, thread: [], error: null }), question: q },
                              }))}
                              className="min-h-[44px] rounded border border-indigo-500/40 bg-indigo-500/10 px-2 py-1 text-ui-xs text-indigo-300 hover:bg-indigo-500/20 transition-colors"
                              data-testid="compute-ask-starter"
                            >
                              {q}
                            </button>
                          ))}
                        </div>
                      )}
                    </form>
                    {ask.error && (
                      <div className="mt-1 text-ui-sm text-red-300" data-testid="compute-ask-error">{ask.error}</div>
                    )}
                    {(ask.thread ?? []).length > 0 && (
                      <div className="mt-1.5 flex flex-col gap-2" data-testid="compute-ask-thread">
                        {(ask.thread ?? []).map((turn, idx) => {
                          const result = turn.result;
                          return (
                            <div key={idx} className="flex flex-col gap-0.5">
                              <div className="text-ui-xs text-neutral-500 font-semibold" data-testid="compute-ask-thread-question">Q: {turn.question}</div>
                              <div className={`rounded border px-2 py-1.5 text-ui-sm ${
                                result.grounding_miss
                                  ? 'border-amber-500/30 bg-amber-500/10'
                                  : 'border-indigo-500/30 bg-indigo-500/10'
                              }`} data-testid="compute-ask-result">
                                {result.grounding_miss && (
                                  <div className="mb-1 flex items-start gap-1.5 text-amber-200/80" data-testid="compute-ask-unverified-caution">
                                    <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                                    <span className="text-ui-xs font-semibold">Unverified answer — no grounding found</span>
                                  </div>
                                )}
                                <div className={`break-words whitespace-pre-wrap ${
                                  result.grounding_miss ? 'text-amber-200' : 'text-indigo-200'
                                }`}>{result.answer}</div>
                                {result.citations.length > 0 && (
                                  <div className="mt-1 flex flex-col gap-0.5" data-testid="compute-ask-citations">
                                    <div className={`text-ui-xs font-semibold ${
                                      result.grounding_miss ? 'text-amber-300/70' : 'text-indigo-300/70'
                                    }`}>Evidence:</div>
                                    {result.citations.map((c) => {
                                      const { label, value } = formatCitation(c);
                                      return (
                                        <div key={c} className={`text-ui-xs rounded px-1.5 py-0.5 ${
                                          result.grounding_miss
                                            ? 'text-amber-300/80 bg-amber-500/20'
                                            : 'text-indigo-300/80 bg-indigo-500/20'
                                        }`}>
                                          <span className="font-semibold">{label}:</span> {value}
                                        </div>
                                      );
                                    })}
                                    <div className="mt-0.5 text-ui-xs text-emerald-300/80" data-testid="compute-ask-grounded">✓ Grounded in run data</div>
                                  </div>
                                )}
                                {!result.citations.length && result.grounding_miss && (
                                  <div className="mt-1 text-ui-xs text-amber-300/70" data-testid="compute-ask-ungrounded">No evidence found for this question</div>
                                )}
                                {!result.citations.length && !result.grounding_miss && (
                                  <div className="mt-1 text-ui-xs text-neutral-400/70 italic" data-testid="compute-ask-no-citation-info">No citations provided</div>
                                )}
                              </div>
                            </div>
                          );
                        })}
                        <button
                          type="button"
                          onClick={() => setAskState((prev) => ({
                            ...prev,
                            [job.id]: { question: '', submitting: false, thread: [], error: null },
                          }))}
                          className="self-start text-ui-xs text-neutral-500 hover:text-neutral-300 transition-colors"
                          data-testid="compute-ask-clear"
                        >
                          Clear conversation
                        </button>
                      </div>
                    )}
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

      {pendingKill && (
        <ConfirmDialog
          title="Kill compute job"
          message={`Kill compute job for "${pendingKill.task_title ?? pendingKill.id}"? This stops the running VM immediately.`}
          confirmLabel="Kill job"
          onConfirm={() => void executeKill(pendingKill)}
          onCancel={() => setPendingKill(null)}
        />
      )}
    </div>
  );
}

export default ComputeJobsPanel;
