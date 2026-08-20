import { useEffect, useState } from 'react';
import {
  fetchRetirementCritiques,
  fetchResearchActionability,
  type RetirementCritique,
  type ActionabilityResponse,
  type CycleHistoryEntry,
} from '../api';

interface StrategicAuditPanelProps {
  projectId: string;
  calibrationFloor?: number;
}

const CALIBRATION_TOOLTIP =
  'HIGH-grade findings should merge more often than LOW. Negative = miscalibrated.';

function calibrationBadgeStyle(
  score: number | null | undefined,
  floor: number,
): { label: string; className: string; tooltip?: string } {
  if (score == null) {
    return {
      label: 'n/a',
      className: 'bg-workbench-panel/60 text-neutral-500 border-neutral-700/50',
    };
  }
  const pct = Math.round(score * 100);
  const sign = pct > 0 ? '+' : '';
  const label = `${sign}${pct}%`;
  if (score >= floor) {
    return {
      label,
      className: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
    };
  }
  if (score >= 0) {
    return {
      label,
      className: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
      tooltip: CALIBRATION_TOOLTIP,
    };
  }
  return {
    label,
    className: 'bg-red-500/20 text-red-400 border-red-500/30',
    tooltip: CALIBRATION_TOOLTIP,
  };
}

function cycleSquareClass(score: number | null, floor: number): string {
  if (score == null) return 'bg-workbench-panel/60 border-neutral-700/50';
  if (score >= floor) return 'bg-emerald-500/40 border-emerald-500/40';
  if (score >= 0) return 'bg-amber-500/40 border-amber-500/40';
  return 'bg-red-500/40 border-red-500/40';
}

function formatRate(rate: number | null | undefined): string {
  if (rate == null) return '—';
  return `${Math.round(rate * 100)}%`;
}

function gradeColor(grade: string) {
  switch (grade) {
    case 'HIGH': return 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
    case 'LOW': return 'bg-red-500/20 text-red-400 border-red-500/30';
    default: return 'bg-amber-500/20 text-amber-400 border-amber-500/30';
  }
}

function GradeDistributionRow({
  grade,
  count,
  tasked,
  merged,
  mergeRate,
}: {
  grade: string;
  count: number;
  tasked: number;
  merged: number;
  mergeRate?: number | null;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui-md">
      <span className={`px-1.5 py-0.5 rounded border text-ui-sm font-medium w-14 text-center ${gradeColor(grade)}`}>
        {grade}
      </span>
      <span className="text-neutral-300 font-mono">{count}</span>
      <span className="text-neutral-500">findings</span>
      {count > 0 && (
        <>
          <span className="text-neutral-600">→</span>
          <span className="text-neutral-300 font-mono">{tasked}</span>
          <span className="text-neutral-500">tasked</span>
          <span className="text-neutral-600">→</span>
          <span className="text-neutral-300 font-mono">{merged}</span>
          <span className="text-neutral-500">merged</span>
          <span className="text-neutral-600">·</span>
          <span className="text-neutral-300 font-mono">{formatRate(mergeRate)}</span>
          <span className="text-neutral-500">merge rate</span>
        </>
      )}
    </div>
  );
}

function CycleHistorySparkline({
  history,
  floor,
}: {
  history: CycleHistoryEntry[];
  floor: number;
}) {
  return (
    <div className="flex items-center gap-0.5 mt-1.5" data-testid="calibration-sparkline">
      {history.map((entry, i) => {
        const score = entry.calibration_score;
        const label =
          score == null
            ? 'n/a'
            : `${score > 0 ? '+' : ''}${Math.round(score * 100)}%`;
        const date = entry.cycle_created_at
          ? new Date(entry.cycle_created_at).toLocaleDateString()
          : entry.cycle_id;
        return (
          <span
            key={`${entry.cycle_id}-${i}`}
            className={`w-3 h-3 rounded border ${cycleSquareClass(score, floor)}`}
            title={`${date}: ${label}`}
          />
        );
      })}
    </div>
  );
}

function CritiqueRow({ critique }: { critique: RetirementCritique }) {
  return (
    <div className="border border-neutral-800 rounded-lg p-3 space-y-1.5 text-ui-md">
      <div className="flex items-center gap-2">
        <span className="text-neutral-500 font-mono text-ui-sm">{critique.direction_id ?? '—'}</span>
        {critique.cycle_created_at && (
          <span className="text-neutral-600 text-ui-sm">
            {new Date(critique.cycle_created_at).toLocaleDateString()}
          </span>
        )}
      </div>
      {critique.what_it_addressed && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-ui-xs">Addressed </span>
          <span className="text-neutral-300 line-clamp-2">{critique.what_it_addressed}</span>
        </div>
      )}
      {critique.pattern_that_produced_it && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-ui-xs">Pattern </span>
          <span className="text-neutral-400 line-clamp-2">{critique.pattern_that_produced_it}</span>
        </div>
      )}
      {critique.lesson && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-ui-xs">Lesson </span>
          <span className="text-neutral-200 line-clamp-2">{critique.lesson}</span>
        </div>
      )}
    </div>
  );
}

export function StrategicAuditPanel({ projectId, calibrationFloor = 0.05 }: StrategicAuditPanelProps) {
  const [critiques, setCritiques] = useState<RetirementCritique[] | null>(null);
  const [actionability, setActionability] = useState<ActionabilityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // loading is derived: true until the first fetch settles for this projectId
  const [settled, setSettled] = useState(false);
  const loading = !settled;

  useEffect(() => {
    let cancelled = false;

    Promise.all([
      fetchRetirementCritiques(projectId),
      fetchResearchActionability(projectId),
    ])
      .then(([crit, action]) => {
        if (cancelled) return;
        setCritiques(crit.critiques);
        setActionability(action);
        setError(null);
        setSettled(true);
      })
      .catch(e => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Failed to load audit data');
        setSettled(true);
      });

    return () => { cancelled = true; };
  }, [projectId]);

  if (loading) {
    return (
      <div className="mt-4 space-y-2">
        <div className="h-5 w-48 animate-pulse rounded bg-workbench-base/50" />
        <div className="h-24 w-full animate-pulse rounded bg-workbench-base/50" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="mt-4 text-ui-md text-red-400 bg-red-900/10 border border-red-800/30 rounded-lg p-3">
        {error}
      </div>
    );
  }

  const grades = ['HIGH', 'MEDIUM', 'LOW'] as const;
  const hasActionability = actionability && actionability.total_findings > 0;
  const hasCritiques = critiques && critiques.length > 0;

  if (!hasActionability && !hasCritiques) return null;

  return (
    <div className="mt-4 space-y-4">
      {hasActionability && (() => {
        const badge = calibrationBadgeStyle(actionability.calibration_score, calibrationFloor);
        const history = actionability.cycle_history ?? [];
        return (
          <div>
            <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>Research Actionability</span>
              <span
                data-testid="calibration-badge"
                className={`px-1.5 py-0.5 rounded border text-ui-sm font-medium normal-case ${badge.className}`}
                title={badge.tooltip}
              >
                {badge.label}
              </span>
              <span className="text-neutral-600 font-normal normal-case">
                ({actionability.cycles_scanned} cycles · {actionability.total_findings} findings)
              </span>
            </h3>
            {history.length > 0 && (
              <CycleHistorySparkline history={history} floor={calibrationFloor} />
            )}
            <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 space-y-2 mt-2">
              {grades.map(g => (
                <GradeDistributionRow
                  key={g}
                  grade={g}
                  count={actionability.grade_distribution[g] ?? 0}
                  tasked={actionability.grade_tasked[g] ?? 0}
                  merged={actionability.grade_merged[g] ?? 0}
                  mergeRate={actionability.grade_merge_rate?.[g] ?? null}
                />
              ))}
            </div>
          </div>
        );
      })()}

      {hasCritiques && (
        <div>
          <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Retirement Critiques
            <span className="ml-2 text-neutral-600 font-normal normal-case">
              ({critiques.length})
            </span>
          </h3>
          <div className="space-y-2">
            {critiques.map((c, i) => (
              <CritiqueRow key={`${c.cycle_id}-${c.direction_id ?? i}`} critique={c} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
