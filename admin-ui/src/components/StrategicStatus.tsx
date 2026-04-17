import { useState, useEffect, useCallback, useRef } from 'react';
import { AlertTriangle, RefreshCw, Compass, CheckCircle2, XCircle, Activity } from 'lucide-react';
import { API_BASE_URL, authHeaders, apiFetch } from '../api';
import { secondsUntil, parseTimestamp } from '../utils/time';

interface StrategicBlockingGuard {
  guard: string;
  reason: string;
  failures?: number;
  decay_at?: string;
}

interface StrategicCycleEntry {
  cycle_id?: string;
  started_at?: string;
  phase1_completed_at?: string | null;
  phase2_completed_at?: string | null;
  outcome?: string;
  failed_phase?: string;
  error?: string;
  directions_created?: number;
  directions_retired?: number;
}

interface StrategicDebug {
  phase: number; // 0=Idle 1=Checking 2=Running 3=Waiting
  blocking_guards: StrategicBlockingGuard[];
  has_purpose: boolean;
  research_spawn_count: number;
  strategic_cycle_count: number;
  strategic_frequency: number;
  architecture_review_frequency: number;
  next_strategic_in: number | null;
  next_is_architecture: boolean;
  strategic_failures: number;
  strategic_failures_at: string | null;
  strategic_failures_decay_at: string | null;
  active_task: {
    id: string;
    phase: string; // 'design_doc_update' | 'strategic_review'
    status: string;
    started_at: string | null;
  } | null;
  last_cycle: StrategicCycleEntry | null;
  recent_cycles: StrategicCycleEntry[];
}

interface Project { project_id: string; project_name: string }

const LIFECYCLE_STEPS = ['Idle', 'Checking', 'Running', 'Waiting'] as const;

function formatSeconds(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function formatTimeAgo(iso: string): string {
  const diff = Date.now() - parseTimestamp(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function phaseLabelForActiveTask(phase: string): string {
  if (phase === 'design_doc_update') return 'Architecture review';
  if (phase === 'strategic_review') return 'Strategic review';
  return phase;
}

function guardLabel(g: StrategicBlockingGuard): string {
  switch (g.guard) {
    case 'strategic_failures_high': {
      const decaySecs = g.decay_at ? secondsUntil(g.decay_at) : null;
      const decayStr = decaySecs && decaySecs > 0 ? ` — auto-recovers in ${formatSeconds(decaySecs)}` : '';
      return `${g.failures ?? '?'} consecutive strategic failures${decayStr}`;
    }
    default:
      return g.reason;
  }
}

function LifecycleIndicator({ debug }: { debug: StrategicDebug }) {
  const activeStep = debug.phase;
  const subLabel = (() => {
    if (activeStep === 2 && debug.active_task) {
      return phaseLabelForActiveTask(debug.active_task.phase);
    }
    if (activeStep === 0 || activeStep === 1) {
      if (debug.next_strategic_in != null) {
        const spawns = debug.next_strategic_in;
        const kind = debug.next_is_architecture ? 'arch' : 'strategic';
        return spawns === 0 ? `${kind} now` : `${kind} in ${spawns} spawn${spawns === 1 ? '' : 's'}`;
      }
    }
    return null;
  })();

  return (
    <div className="mt-2">
      <p className="text-[10px] font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">
        Cycle
      </p>
      <div className="flex items-center w-full">
        {LIFECYCLE_STEPS.map((step, i) => {
          const isActive = i === activeStep;
          const isPast = i < activeStep;
          return (
            <div key={step} className="flex items-center flex-1 min-w-0">
              <div className="flex flex-col items-center shrink-0">
                <div
                  className={`w-2 h-2 rounded-full border transition-colors ${
                    isActive
                      ? 'bg-cyan-400 border-cyan-400 ring-2 ring-cyan-400/30'
                      : isPast
                      ? 'bg-neutral-600 border-neutral-600'
                      : 'bg-transparent border-neutral-600'
                  }`}
                />
                <span
                  className={`text-[9px] mt-0.5 leading-tight text-center whitespace-nowrap ${
                    isActive ? 'text-cyan-400 font-semibold' : 'text-neutral-600'
                  }`}
                >
                  {step}
                  {isActive && subLabel && (
                    <span className="block text-neutral-600 font-normal">{subLabel}</span>
                  )}
                </span>
              </div>
              {i < LIFECYCLE_STEPS.length - 1 && (
                <div className={`flex-1 h-px mx-0.5 ${i < activeStep ? 'bg-neutral-600' : 'bg-neutral-800'}`} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function outcomeBadge(outcome: string | undefined) {
  if (outcome === 'success' || outcome === 'completed') {
    return <span className="inline-flex items-center gap-1 text-emerald-400 text-[10px]"><CheckCircle2 className="w-3 h-3" />success</span>;
  }
  if (outcome === 'in_progress') {
    return <span className="inline-flex items-center gap-1 text-cyan-400 text-[10px]"><Activity className="w-3 h-3" />in progress</span>;
  }
  if (outcome) {
    return <span className="inline-flex items-center gap-1 text-red-400 text-[10px]"><XCircle className="w-3 h-3" />{outcome}</span>;
  }
  return null;
}

export function StrategicStatus() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [debugData, setDebugData] = useState<Record<string, StrategicDebug>>({});
  const [expanded, setExpanded] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const headers = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/autonomous-status`, {
        headers: { Authorization: headers.Authorization },
      });
      if (!res.ok) return;
      const data = await res.json();
      const fetched: Project[] = (data.projects || []).map((p: { project_id: string; project_name: string }) => ({
        project_id: p.project_id,
        project_name: p.project_name,
      }));
      setProjects(fetched);

      const settled = await Promise.allSettled(
        fetched.map(p =>
          apiFetch(`/api/admin/projects/${p.project_id}/strategic-debug`)
            .then(r => (r.ok ? r.json() : null))
            .then((d): [string, StrategicDebug] | null => (d ? [p.project_id, d as StrategicDebug] : null))
            .catch(() => null)
        )
      );
      const next: Record<string, StrategicDebug> = {};
      for (const r of settled) {
        if (r.status === 'fulfilled' && r.value) {
          const [id, d] = r.value;
          next[id] = d;
        }
      }
      setDebugData(next);
      setLastUpdated(new Date());
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 30000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  useEffect(() => {
    if (!expanded) return;
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setExpanded(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [expanded]);

  // Headline state: any project running? any blocked?
  const anyRunning = Object.values(debugData).some(d => d.phase === 2);
  const anyBlocked = Object.values(debugData).some(d => d.phase === 3);
  const headlineColor = anyRunning ? 'text-cyan-400' : anyBlocked ? 'text-amber-400' : 'text-neutral-500';
  const headlineLabel = anyRunning ? 'Running' : anyBlocked ? 'Waiting' : 'Idle';

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex flex-col items-center cursor-pointer group"
        title="Strategic cycle status"
      >
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Strategic</span>
        <span className={`text-xl font-bold font-mono leading-none flex items-center gap-1.5 ${headlineColor}`}>
          <Compass className="w-4 h-4" />
          <span>{headlineLabel}</span>
        </span>
      </button>

      {expanded && (
        <div className="absolute top-full mt-2 right-0 w-96 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[70vh] overflow-y-auto custom-scrollbar">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Strategic Cycle</h3>
            <span className="flex items-center gap-1.5">
              {lastUpdated && (
                <span className="text-[10px] text-neutral-600 font-mono">
                  {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
              )}
              <button onClick={fetchStatus} className="text-neutral-500 hover:text-neutral-300 transition-colors">
                <RefreshCw className="w-3 h-3" />
              </button>
            </span>
          </div>

          {projects.length === 0 && (
            <p className="text-xs text-neutral-600">No projects found</p>
          )}

          {projects.map(p => {
            const debug = debugData[p.project_id];
            return (
              <div key={p.project_id} className="mb-3 last:mb-0 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-neutral-200 truncate max-w-[200px]">{p.project_name}</span>
                </div>

                {!debug && (
                  <p className="text-[11px] text-neutral-600">Loading…</p>
                )}

                {debug && (
                  <div className="space-y-1.5">
                    {/* Active task */}
                    {debug.active_task && (
                      <div className="flex items-center justify-between text-[11px]">
                        <span className="text-neutral-500 flex items-center gap-1">
                          <Activity className="w-3 h-3 shrink-0" />
                          {phaseLabelForActiveTask(debug.active_task.phase)}
                        </span>
                        <span className="text-cyan-400 font-mono text-[10px]">{debug.active_task.status}</span>
                      </div>
                    )}

                    {/* Trigger countdown */}
                    {!debug.active_task && debug.next_strategic_in != null && (
                      <div className="flex items-center justify-between text-[11px] text-neutral-500">
                        <span>Next cycle</span>
                        <span className="font-mono">
                          {debug.next_strategic_in === 0 ? 'ready now' : `${debug.next_strategic_in} spawn${debug.next_strategic_in === 1 ? '' : 's'} away`}
                          {debug.next_is_architecture && <span className="text-amber-400 ml-1">(arch)</span>}
                        </span>
                      </div>
                    )}

                    {/* Spawn count + cycle count */}
                    <div className="flex items-center justify-between text-[11px] text-neutral-500">
                      <span>Spawns · cycles</span>
                      <span className="font-mono">
                        {debug.research_spawn_count} · {debug.strategic_cycle_count}
                      </span>
                    </div>

                    {/* Last cycle */}
                    {debug.last_cycle && (
                      <div className="flex items-center justify-between text-[11px] text-neutral-500">
                        <span>Last cycle</span>
                        <span className="flex items-center gap-1.5 font-mono text-[10px]">
                          {outcomeBadge(debug.last_cycle.outcome)}
                          {debug.last_cycle.phase2_completed_at && (
                            <span className="text-neutral-600">{formatTimeAgo(debug.last_cycle.phase2_completed_at)}</span>
                          )}
                        </span>
                      </div>
                    )}

                    {/* Purpose warning */}
                    {!debug.has_purpose && (
                      <div className="rounded-md bg-amber-500/5 border border-amber-500/20 px-2 py-1 flex items-start gap-1.5">
                        <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400 mt-0.5" />
                        <span className="text-[11px] leading-tight text-amber-300">
                          Vision has no purpose — strategic cycles will bootstrap once, then stall on failure.
                        </span>
                      </div>
                    )}

                    {/* Blocking guards */}
                    {debug.blocking_guards.length > 0 && (
                      <div className="mt-2 rounded-md bg-red-500/5 border border-red-500/20 px-2.5 py-2">
                        <p className="text-[10px] font-semibold text-red-400 uppercase tracking-wider mb-1.5">
                          Why strategic is blocked
                        </p>
                        <ul className="space-y-1">
                          {debug.blocking_guards.map((g, i) => (
                            <li key={i} className="flex items-start gap-1.5">
                              <AlertTriangle className="w-3 h-3 shrink-0 text-red-400 mt-px" />
                              <span className="text-[11px] leading-tight text-red-300">{guardLabel(g)}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <LifecycleIndicator debug={debug} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
