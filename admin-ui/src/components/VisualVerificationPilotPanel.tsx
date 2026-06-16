import { useCallback, useEffect, useState } from 'react';
import type { Project } from '../types/project';
import { API_BASE_URL, fetchVisualPilotStatus, type VisualPilotStatus } from '../api';

interface Props {
  project: Project;
  onNavigateToTask?: (taskId: string) => void;
}

interface ShotPair {
  key: string;
  before?: string;
  after?: string;
}

function buildShotPairs(names: string[]): ShotPair[] {
  const map = new Map<string, ShotPair>();
  for (const name of names) {
    let key: string;
    let phase: 'before' | 'after' | null = null;
    if (name.endsWith('-before')) {
      key = name.slice(0, -'-before'.length);
      phase = 'before';
    } else if (name.endsWith('-after')) {
      key = name.slice(0, -'-after'.length);
      phase = 'after';
    } else {
      key = name;
    }
    let pair = map.get(key);
    if (!pair) {
      pair = { key };
      map.set(key, pair);
    }
    if (phase === 'before') pair.before = name;
    else if (phase === 'after') pair.after = name;
  }
  return Array.from(map.values());
}

function PilotShotImage({
  projectId,
  taskId,
  name,
}: {
  projectId: string;
  taskId: string;
  name: string;
}) {
  const [error, setError] = useState(false);
  const src = `${API_BASE_URL}/api/admin/viewport-shots/download-url?${new URLSearchParams({
    project_id: projectId,
    task_id: taskId,
    name,
  }).toString()}`;
  if (error) return <p className="text-[10px] text-red-400 break-words">Failed to load screenshot</p>;
  return (
    <img
      src={src}
      alt={name}
      className="max-w-full rounded border border-neutral-700"
      onError={() => setError(true)}
    />
  );
}

function PilotStatusBadge({ status }: { status: VisualPilotStatus }) {
  const { entry_count, admin_task_count, days_elapsed, thresholds, infra_rate } = status;

  if (entry_count === 0 && admin_task_count === 0) {
    return (
      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-neutral-500/15 text-neutral-400 border border-neutral-500/30">
        NOT STARTED
      </span>
    );
  }

  const isKillWindow =
    entry_count >= thresholds.target_admin_tasks || days_elapsed >= thresholds.target_days;
  const isEarlyPauseRisk = infra_rate >= thresholds.early_pause_infra_rate;

  if (isEarlyPauseRisk) {
    return (
      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">
        EARLY PAUSE RISK
      </span>
    );
  }
  if (isKillWindow) {
    return (
      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/20">
        KILL WINDOW
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-sky-500/15 text-sky-300 border border-sky-500/20">
      RUNNING
    </span>
  );
}

function ProgressBar({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-[10px] text-neutral-500 shrink-0 w-28">{label}</span>
      <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
        <div
          className="h-full bg-sky-500/60 rounded-full"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] text-neutral-400 shrink-0 tabular-nums">
        {value} / {max}
      </span>
    </div>
  );
}

export function VisualVerificationPilotPanel({ project, onNavigateToTask }: Props) {
  const [status, setStatus] = useState<VisualPilotStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedShots, setExpandedShots] = useState<Set<string>>(new Set());

  const load = useCallback(
    async (signal: AbortSignal) => {
      setLoading(true);
      setError(null);
      setStatus(null);
      try {
        const data = await fetchVisualPilotStatus(project.id, signal);
        setStatus(data);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        setError(e instanceof Error ? e.message : 'Failed to load pilot status');
      } finally {
        setLoading(false);
      }
    },
    [project.id],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (loading || (!status && !error)) return null;

  if (error) {
    return (
      <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
        <div className="text-[11px] font-bold text-neutral-300 uppercase tracking-widest mb-1">
          Visual Verification Pilot
        </div>
        <p className="text-[10px] text-red-300">{error}</p>
      </div>
    );
  }

  if (!status) return null;

  const {
    admin_task_count,
    entry_count,
    malformed_count,
    pairs_inspected,
    pairs_changed,
    infra_total,
    infra_rate,
    days_elapsed,
    caught_count,
    caught,
    thresholds,
  } = status;

  return (
    <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
      <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
        <div className="text-[11px] font-bold text-neutral-300 uppercase tracking-widest">
          Visual Verification Pilot
        </div>
        <PilotStatusBadge status={status} />
      </div>

      <div className="space-y-1.5 mb-2">
        <ProgressBar
          value={entry_count}
          max={thresholds.target_admin_tasks}
          label="admin-ui PRs"
        />
        <ProgressBar
          value={days_elapsed}
          max={thresholds.target_days}
          label="days elapsed"
        />
      </div>

      <div className="flex flex-wrap gap-x-3 gap-y-0.5 mb-2">
        <span className="text-[10px] text-neutral-500">
          Inspected <span className="text-neutral-300 tabular-nums">{pairs_inspected}</span>
          {' '}· Changed <span className="text-neutral-300 tabular-nums">{pairs_changed}</span>
        </span>
        <span className="text-[10px] text-neutral-500">
          Tasks <span className="text-neutral-300 tabular-nums">{entry_count}/{admin_task_count}</span>
        </span>
        <span className="text-[10px] text-neutral-500">
          Infra failures <span className="text-neutral-300 tabular-nums">{infra_total}</span>
          {' '}(<span className="text-neutral-300 tabular-nums">{(infra_rate * 100).toFixed(0)}%</span>)
        </span>
      </div>

      {malformed_count > 0 && (
        <p className="text-[10px] text-amber-300 mb-2">
          {malformed_count} malformed {malformed_count === 1 ? 'entry' : 'entries'}
        </p>
      )}

      <div className="border-t border-neutral-800/60 pt-2">
        <div className="text-[10px] font-semibold text-neutral-400 mb-1.5">
          Caught defects ({caught_count})
        </div>
        {caught_count === 0 ? (
          <p className="text-[10px] text-neutral-600">
            No defects caught beyond assertion gates yet.
          </p>
        ) : (
          <div className="space-y-1">
            {caught.map((entry) => {
              const hasShots = entry.viewport_shot_names.length > 0;
              const shotsExpanded = expandedShots.has(entry.task_id);
              const pairs = hasShots ? buildShotPairs(entry.viewport_shot_names) : [];
              return (
                <div
                  key={entry.task_id}
                  className="rounded border border-neutral-800/60 bg-neutral-900/50"
                >
                  <button
                    onClick={() => onNavigateToTask?.(entry.task_id)}
                    className="w-full text-left px-2 py-1.5 hover:bg-neutral-800/60 transition-colors min-h-[44px] flex flex-col justify-center"
                  >
                    <span className="text-[11px] text-neutral-200 break-words">{entry.task_title}</span>
                    {entry.note && (
                      <span className="text-[10px] text-neutral-500 break-words">{entry.note}</span>
                    )}
                  </button>
                  {hasShots && (
                    <div className="px-2 pb-1.5">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setExpandedShots((prev) => {
                            const next = new Set(prev);
                            if (next.has(entry.task_id)) next.delete(entry.task_id);
                            else next.add(entry.task_id);
                            return next;
                          });
                        }}
                        className="text-[10px] text-sky-400 hover:text-sky-300 transition-colors"
                      >
                        {shotsExpanded
                          ? 'Hide screenshots'
                          : `View ${entry.viewport_shot_names.length} screenshot${entry.viewport_shot_names.length !== 1 ? 's' : ''}`}
                      </button>
                      {shotsExpanded && (
                        <div className="mt-1.5 space-y-2">
                          {pairs.map((pair) => (
                            <div key={pair.key} className="space-y-1">
                              <p className="text-[10px] font-mono text-neutral-500 truncate">{pair.key}</p>
                              {pair.before && pair.after ? (
                                <div className="grid grid-cols-2 gap-2">
                                  <div>
                                    <p className="text-[10px] text-neutral-500 mb-0.5">Before</p>
                                    <PilotShotImage
                                      projectId={project.id}
                                      taskId={entry.task_id}
                                      name={pair.before}
                                    />
                                  </div>
                                  <div>
                                    <p className="text-[10px] text-neutral-500 mb-0.5">After</p>
                                    <PilotShotImage
                                      projectId={project.id}
                                      taskId={entry.task_id}
                                      name={pair.after}
                                    />
                                  </div>
                                </div>
                              ) : (
                                <PilotShotImage
                                  projectId={project.id}
                                  taskId={entry.task_id}
                                  name={pair.before ?? pair.after ?? pair.key}
                                />
                              )}
                            </div>
                          ))}
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
    </div>
  );
}
