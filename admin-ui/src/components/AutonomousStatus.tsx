import { useState, useEffect, useCallback, useRef, type RefObject } from 'react';
import { Zap, ZapOff, AlertTriangle, RefreshCw, Lock, Clock, PlayCircle, Gauge, Pause, Server } from 'lucide-react';
import { API_BASE_URL, authHeaders, apiFetch } from '../api';
import { secondsUntil, parseTimestamp } from '../utils/time';

interface ProjectAutonomousStatus {
  project_id: string;
  project_name: string;
  autonomous_enabled: boolean;
  consecutive_failures: number;
  last_research_at: string | null;
  tasks_spawned_last_hour: number;
  rate_limit: number;
}

interface BlockingGuard {
  guard: string;
  reason: string;
  // circuit_breaker_open
  opened_at?: string;
  cooldown_ends_at?: string;
  failure_count?: number;
  // no_eligible_directions
  time_until_eligible_seconds?: number | null;
  busy_direction_count?: number;
  total_active_directions?: number;
  // research_task_active
  task_id?: string;
  active_research_count?: number;
  max_parallel?: number;
  // rate_limit_exceeded
  tasks_this_hour?: number;
  max_per_hour?: number;
  // waiting_for_interval
  next_check_at?: string;
  seconds_until_check?: number;
  // vm_slots_full
  vm_consuming?: number;
  max_concurrent_vms?: number;
  // pipeline_paused
  pause_reason?: string | null;
  paused_at?: string | null;
}

interface CircuitBreakerInfo {
  state: string; // CLOSED | OPEN | HALF_OPEN
  failure_count: number;
  success_count: number;
  opened_at: string | null;
  cooldown_seconds: number;
  cooldown_ends_at: string | null;
}

interface AutonomyDebug {
  spawn_blocked: boolean;
  blocking_guards: BlockingGuard[];
  circuit_breaker: CircuitBreakerInfo;
  rate_limit: {
    tasks_this_hour: number;
    max_per_hour: number;
  };
  directions: Array<{
    id: string;
    text: string;
    status: string;
    last_researched_at: string | null;
    cooldown_minutes: number;
    is_eligible: boolean;
    seconds_until_eligible: number | null;
  }>;
  next_check_at: string;
}

interface AutonomousStatusProps {
  onRefreshTasks?: () => void;
  wsRef?: RefObject<WebSocket | null>;
  pauseReason?: string | null;
}

const LIFECYCLE_STEPS = ['Idle', 'Checking', 'Spawning', 'Executing', 'Cooldowns'] as const;

function getLifecyclePhase(debug: AutonomyDebug): number {
  const guards = debug.blocking_guards.map(g => g.guard);
  if (guards.includes('vm_slots_full') || guards.includes('research_task_active')) return 3; // Executing
  if (guards.includes('no_eligible_directions') || guards.includes('rate_limit_exceeded')) return 4; // Cooldowns
  if (guards.includes('circuit_breaker_open') || guards.includes('pipeline_paused')) return 4; // Cooldowns
  if (guards.includes('waiting_for_interval')) return 0; // Idle — waiting for next check window
  if (!debug.spawn_blocked) return 1; // Ready to check/spawn
  return 0; // Idle (disabled or other block)
}

function formatSeconds(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}


function CircuitBreakerPill({ state }: { state: string }) {
  const styles: Record<string, string> = {
    CLOSED: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
    OPEN: 'bg-red-500/15 text-red-400 border border-red-500/30',
    HALF_OPEN: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
  };
  return (
    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded font-mono ${styles[state] ?? 'bg-neutral-700 text-neutral-400'}`}>
      {state}
    </span>
  );
}

function GuardIcon({ guard }: { guard: string }) {
  switch (guard) {
    case 'autonomous_disabled':
      return <Lock className="w-3 h-3 shrink-0 text-neutral-500" />;
    case 'circuit_breaker_open':
      return <Zap className="w-3 h-3 shrink-0 text-red-400" />;
    case 'no_eligible_directions':
      return <Clock className="w-3 h-3 shrink-0 text-amber-400" />;
    case 'research_task_active':
      return <PlayCircle className="w-3 h-3 shrink-0 text-blue-400" />;
    case 'rate_limit_exceeded':
      return <Gauge className="w-3 h-3 shrink-0 text-orange-400" />;
    case 'waiting_for_interval':
      return <Clock className="w-3 h-3 shrink-0 text-neutral-400" />;
    case 'vm_slots_full':
      return <Server className="w-3 h-3 shrink-0 text-blue-400" />;
    case 'pipeline_paused':
      return <Pause className="w-3 h-3 shrink-0 text-amber-400" />;
    case 'no_available_accounts':
      return <AlertTriangle className="w-3 h-3 shrink-0 text-red-400" />;
    default:
      return <AlertTriangle className="w-3 h-3 shrink-0 text-neutral-400" />;
  }
}

function guardColor(guard: string): string {
  switch (guard) {
    case 'autonomous_disabled': return 'text-neutral-400';
    case 'circuit_breaker_open': return 'text-red-400';
    case 'no_eligible_directions': return 'text-amber-400';
    case 'research_task_active': return 'text-blue-400';
    case 'rate_limit_exceeded': return 'text-orange-400';
    case 'waiting_for_interval': return 'text-neutral-400';
    case 'vm_slots_full': return 'text-blue-400';
    case 'pipeline_paused': return 'text-amber-400';
    case 'no_available_accounts': return 'text-red-400';
    default: return 'text-neutral-400';
  }
}

function guardLabel(g: BlockingGuard): string {
  switch (g.guard) {
    case 'autonomous_disabled':
      return 'Autonomy is disabled';
    case 'circuit_breaker_open': {
      const mins = g.cooldown_ends_at ? Math.ceil(secondsUntil(g.cooldown_ends_at) / 60) : '?';
      return `Circuit breaker OPEN — resets in ${mins}m (${g.failure_count ?? '?'} consecutive failures)`;
    }
    case 'no_eligible_directions': {
      const busy = g.busy_direction_count ?? 0;
      const total = g.total_active_directions ?? 0;
      if (total === 0) return 'No active directions exist';
      if (busy > 0 && busy === total) return `All ${total} directions held by running research`;
      if (g.time_until_eligible_seconds != null) {
        const waitStr = formatSeconds(g.time_until_eligible_seconds);
        return busy > 0
          ? `${busy}/${total} held by running research; others cool down in ${waitStr}`
          : `All directions in cooldown — next eligible in ${waitStr}`;
      }
      return g.reason;
    }
    case 'research_task_active':
      return `Parallel research cap reached (${g.active_research_count ?? '?'}/${g.max_parallel ?? '?'})`;
    case 'rate_limit_exceeded':
      return `Rate limit: ${g.tasks_this_hour ?? '?'}/${g.max_per_hour ?? '?'} tasks this hour`;
    case 'waiting_for_interval': {
      const secs = g.seconds_until_check ?? (g.next_check_at ? secondsUntil(g.next_check_at) : 0);
      if (secs <= 0) return 'Research eligible, waiting for VM slot';
      return `Next research check in ${formatSeconds(secs)}`;
    }
    case 'vm_slots_full':
      return `VM slots full (${g.vm_consuming ?? '?'}/${g.max_concurrent_vms ?? '?'}) — waiting for a task to finish`;
    case 'pipeline_paused':
      return `Pipeline paused${g.pause_reason ? ` — ${g.pause_reason}` : ''}`;
    case 'no_available_accounts':
      return 'No Max Plan accounts available for research model';
    default:
      return g.reason;
  }
}

function SpawnGuardDiagnostics({ guards }: { guards: BlockingGuard[] }) {
  // waiting_for_interval is normal operation — shown in the lifecycle indicator instead
  const errorGuards = guards.filter(g => g.guard !== 'waiting_for_interval');
  if (errorGuards.length === 0) return null;
  return (
    <div className="mt-2 rounded-md bg-red-500/5 border border-red-500/20 px-2.5 py-2">
      <p className="text-[10px] font-semibold text-red-400 uppercase tracking-wider mb-1.5">
        Why autonomy isn't spawning
      </p>
      <ul className="space-y-1">
        {errorGuards.map((g, i) => (
          <li key={i} className="flex items-start gap-1.5">
            <span className="mt-px"><GuardIcon guard={g.guard} /></span>
            <span className={`text-[11px] leading-tight ${guardColor(g.guard)}`}>
              {guardLabel(g)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CircuitBreakerDetail({ cb }: { cb: CircuitBreakerInfo }) {
  const secsLeft = cb.cooldown_ends_at ? secondsUntil(cb.cooldown_ends_at) : null;
  return (
    <div className="flex items-center justify-between text-[11px]">
      <span className="text-neutral-500 flex items-center gap-1">
        <Zap className="w-3 h-3 shrink-0" />
        Circuit breaker
      </span>
      <span className="flex items-center gap-1.5">
        <CircuitBreakerPill state={cb.state} />
        {cb.failure_count > 0 && (
          <span className="text-neutral-500 font-mono">{cb.failure_count}f</span>
        )}
        {cb.state === 'OPEN' && secsLeft != null && secsLeft > 0 && (
          <span className="text-red-400 font-mono text-[10px]">-{formatSeconds(secsLeft)}</span>
        )}
      </span>
    </div>
  );
}

function LifecycleIndicator({ debug }: { debug: AutonomyDebug }) {
  const activeStep = getLifecyclePhase(debug);
  const intervalGuard = debug.blocking_guards.find(g => g.guard === 'waiting_for_interval');
  const nextCheckSecs = intervalGuard
    ? (intervalGuard.seconds_until_check ?? secondsUntil(intervalGuard.next_check_at ?? debug.next_check_at))
    : secondsUntil(debug.next_check_at);

  return (
    <div className="mt-2">
      <p className="text-[10px] font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">
        Loop lifecycle
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
                      ? 'bg-emerald-400 border-emerald-400 ring-2 ring-emerald-400/30'
                      : isPast
                      ? 'bg-neutral-600 border-neutral-600'
                      : 'bg-transparent border-neutral-600'
                  }`}
                />
                <span
                  className={`text-[9px] mt-0.5 leading-tight text-center whitespace-nowrap ${
                    isActive ? 'text-emerald-400 font-semibold' : 'text-neutral-600'
                  }`}
                >
                  {step}
                  {step === 'Idle' && isActive && nextCheckSecs > 0 && (
                    <span className="block text-neutral-600 font-normal">{formatSeconds(nextCheckSecs)}</span>
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

export function AutonomousStatus({ onRefreshTasks, wsRef, pauseReason }: AutonomousStatusProps) {
  const [projects, setProjects] = useState<ProjectAutonomousStatus[]>([]);
  const [debugData, setDebugData] = useState<Record<string, AutonomyDebug>>({});
  const [expanded, setExpanded] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [resettingBreaker, setResettingBreaker] = useState<string | null>(null);
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
      const fetched: ProjectAutonomousStatus[] = data.projects || [];
      setProjects(fetched);

      // Fetch debug data for each project in parallel
      const settled = await Promise.allSettled(
        fetched.map(p =>
          apiFetch(`/api/admin/projects/${p.project_id}/autonomy-debug`)
            .then(r => (r.ok ? r.json() : null))
            .then((d): [string, AutonomyDebug] | null => (d ? [p.project_id, d as AutonomyDebug] : null))
            .catch(() => null)
        )
      );
      const newDebug: Record<string, AutonomyDebug> = {};
      for (const result of settled) {
        if (result.status === 'fulfilled' && result.value) {
          const [id, d] = result.value;
          newDebug[id] = d;
        }
      }
      setDebugData(newDebug);
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

  // Close dropdown on outside click
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

  // Subscribe to WebSocket autonomous events for real-time updates.
  useEffect(() => {
    if (!wsRef) return;
    let attachedWs: WebSocket | null = null;

    const handleMessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.event === 'autonomous_activity' || msg.event === 'autonomous_paused') {
          fetchStatus();
        }
      } catch {
        // ignore parse errors
      }
    };

    const attach = () => {
      const currentWs = wsRef.current;
      if (currentWs === attachedWs) return;
      if (attachedWs) attachedWs.removeEventListener('message', handleMessage);
      attachedWs = currentWs;
      if (currentWs) currentWs.addEventListener('message', handleMessage);
    };

    attach();
    const interval = setInterval(attach, 5000);
    return () => {
      clearInterval(interval);
      if (attachedWs) attachedWs.removeEventListener('message', handleMessage);
    };
  }, [wsRef, fetchStatus]);

  const resetCircuitBreaker = async (projectId: string) => {
    setResettingBreaker(projectId);
    try {
      const res = await apiFetch(`/api/admin/projects/${projectId}/circuit-breaker/reset`, { method: 'POST' });
      if (res.ok) {
        await fetchStatus();
      }
    } catch {
      // silent
    } finally {
      setResettingBreaker(null);
    }
  };

  const toggleAutonomy = async (projectId: string, enabled: boolean) => {
    setToggling(projectId);
    try {
      const headers = await authHeaders();
      await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}/autonomous`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      await fetchStatus();
      onRefreshTasks?.();
    } catch {
      // silent
    } finally {
      setToggling(null);
    }
  };

  const anyEnabled = projects.some(p => p.autonomous_enabled);
  const anyCircuitBreaker = projects.some(p => p.consecutive_failures >= 2);

  const formatTimeAgo = (iso: string) => {
    const diff = Date.now() - parseTimestamp(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex flex-col items-center cursor-pointer group"
        title="Autonomous Pipeline Status"
      >
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Autonomy</span>
        <span className="text-xl font-bold font-mono leading-none flex items-center gap-1.5">
          {anyEnabled ? (
            <>
              <Zap className={`w-4 h-4 ${anyCircuitBreaker ? 'text-amber-400' : 'text-emerald-400'}`} />
              <span className={anyCircuitBreaker ? 'text-amber-400' : 'text-emerald-400'}>
                {anyCircuitBreaker ? 'Warn' : 'Active'}
              </span>
            </>
          ) : (
            <>
              <ZapOff className="w-4 h-4 text-neutral-500" />
              <span className="text-neutral-500">Off</span>
            </>
          )}
        </span>
      </button>

      {expanded && (
        <div className="absolute top-full mt-2 right-0 w-96 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[70vh] overflow-y-auto custom-scrollbar">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Autonomous Pipeline</h3>
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

          {pauseReason && (
            <div className="mb-3 rounded-md bg-amber-500/10 border border-amber-500/25 px-2.5 py-2 flex items-start gap-1.5">
              <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400 mt-0.5" />
              <span className="text-[11px] text-amber-300 leading-tight">{pauseReason}</span>
            </div>
          )}

          {projects.length === 0 && (
            <p className="text-xs text-neutral-600">No projects found</p>
          )}

          {projects.map(p => {
            const debug = debugData[p.project_id];
            return (
              <div key={p.project_id} className="mb-3 last:mb-0 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
                {/* Header */}
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-neutral-200 truncate max-w-[200px]">{p.project_name}</span>
                  <button
                    onClick={() => toggleAutonomy(p.project_id, !p.autonomous_enabled)}
                    disabled={toggling === p.project_id}
                    className={`text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded transition-colors ${
                      p.autonomous_enabled
                        ? 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'
                        : 'bg-neutral-800 text-neutral-500 hover:bg-neutral-700'
                    } ${toggling === p.project_id ? 'opacity-50' : ''}`}
                  >
                    {p.autonomous_enabled ? 'Enabled' : 'Disabled'}
                  </button>
                </div>

                <div className="space-y-1.5">
                  {/* Circuit breaker — enhanced with state pill when debug available */}
                  {debug ? (
                    <>
                      <CircuitBreakerDetail cb={debug.circuit_breaker} />
                      {(debug.circuit_breaker.state === 'OPEN' || debug.circuit_breaker.state === 'HALF_OPEN') && (
                        <div className="flex justify-end">
                          <button
                            onClick={() => resetCircuitBreaker(p.project_id)}
                            disabled={resettingBreaker === p.project_id}
                            className={`text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded transition-colors bg-red-500/10 text-red-400 hover:bg-red-500/20 ${resettingBreaker === p.project_id ? 'opacity-50' : ''}`}
                          >
                            {resettingBreaker === p.project_id ? 'Resetting…' : 'Reset Circuit Breaker'}
                          </button>
                        </div>
                      )}
                    </>
                  ) : p.consecutive_failures > 0 ? (
                    <div className="flex items-center gap-1.5 text-[11px]">
                      <AlertTriangle className={`w-3 h-3 shrink-0 ${p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}`} />
                      <span className={p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}>
                        {p.consecutive_failures} consecutive failure{p.consecutive_failures !== 1 ? 's' : ''}
                        {p.consecutive_failures >= 3 && ' — circuit breaker tripped'}
                      </span>
                    </div>
                  ) : null}

                  {/* Last research */}
                  <div className="flex items-center justify-between text-[11px] text-neutral-500">
                    <span>Last research</span>
                    <span className="font-mono">{p.last_research_at ? formatTimeAgo(p.last_research_at) : 'never'}</span>
                  </div>

                  {/* Rate limit usage */}
                  <div className="flex items-center justify-between text-[11px] text-neutral-500">
                    <span>Tasks this hour</span>
                    <span className="font-mono">
                      <span className={p.tasks_spawned_last_hour >= p.rate_limit ? 'text-amber-400' : ''}>
                        {p.tasks_spawned_last_hour}
                      </span>
                      /{p.rate_limit}
                    </span>
                  </div>
                </div>

                {/* Debug sections — only when debug data available */}
                {debug && (
                  <>
                    {/* Spawn guard diagnostics */}
                    {debug.spawn_blocked && (
                      <SpawnGuardDiagnostics guards={debug.blocking_guards} />
                    )}

                    {/* Lifecycle step indicator */}
                    <LifecycleIndicator debug={debug} />
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
