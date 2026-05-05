import { useEffect, useState } from 'react';
import {
  fetchRetirementCritiques,
  fetchResearchActionability,
  type RetirementCritique,
  type ActionabilityResponse,
} from '../api';

interface StrategicAuditPanelProps {
  projectId: string;
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
}: {
  grade: string;
  count: number;
  tasked: number;
  merged: number;
}) {
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className={`px-1.5 py-0.5 rounded border text-[10px] font-medium w-14 text-center ${gradeColor(grade)}`}>
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
        </>
      )}
    </div>
  );
}

function CritiqueRow({ critique }: { critique: RetirementCritique }) {
  return (
    <div className="border border-neutral-800 rounded-lg p-3 space-y-1.5 text-[11px]">
      <div className="flex items-center gap-2">
        <span className="text-neutral-500 font-mono text-[10px]">{critique.direction_id ?? '—'}</span>
        {critique.cycle_created_at && (
          <span className="text-neutral-600 text-[10px]">
            {new Date(critique.cycle_created_at).toLocaleDateString()}
          </span>
        )}
      </div>
      {critique.what_it_addressed && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-[9px]">Addressed </span>
          <span className="text-neutral-300 line-clamp-2">{critique.what_it_addressed}</span>
        </div>
      )}
      {critique.pattern_that_produced_it && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-[9px]">Pattern </span>
          <span className="text-neutral-400 line-clamp-2">{critique.pattern_that_produced_it}</span>
        </div>
      )}
      {critique.lesson && (
        <div>
          <span className="text-neutral-500 uppercase tracking-wide text-[9px]">Lesson </span>
          <span className="text-neutral-200 line-clamp-2">{critique.lesson}</span>
        </div>
      )}
    </div>
  );
}

export function StrategicAuditPanel({ projectId }: StrategicAuditPanelProps) {
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
        <div className="h-5 w-48 animate-pulse rounded bg-neutral-900/50" />
        <div className="h-24 w-full animate-pulse rounded bg-neutral-900/50" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="mt-4 text-[11px] text-red-400 bg-red-900/10 border border-red-800/30 rounded-lg p-3">
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
      {hasActionability && (
        <div>
          <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
            Research Actionability
            <span className="ml-2 text-neutral-600 font-normal normal-case">
              ({actionability.cycles_scanned} cycles · {actionability.total_findings} findings)
            </span>
          </h3>
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 space-y-2">
            {grades.map(g => (
              <GradeDistributionRow
                key={g}
                grade={g}
                count={actionability.grade_distribution[g] ?? 0}
                tasked={actionability.grade_tasked[g] ?? 0}
                merged={actionability.grade_merged[g] ?? 0}
              />
            ))}
          </div>
        </div>
      )}

      {hasCritiques && (
        <div>
          <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
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
