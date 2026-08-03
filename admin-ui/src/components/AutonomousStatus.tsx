/* eslint-disable react-hooks/purity, react-hooks/refs */
import { useState, useEffect, useCallback, useRef, type RefObject } from 'react';
import { Zap, ZapOff, AlertTriangle, RefreshCw, Lock, Clock, PlayCircle, Gauge, Pause, Server, Compass, CheckCircle2, XCircle, Activity, FileText } from 'lucide-react';
import { API_BASE_URL, authHeaders, apiFetch } from '../api';
import { secondsUntil, parseTimestamp, snapshotFreshness } from '../utils/time';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { resolveAccess, type ProjectRole } from '../accessControl';

// Matches _MAX_PAYLOAD_BYTES from backend/app/payload_schemas.py
const MAX_PAYLOAD_KB = 190;

// A last-successful poll older than this many seconds is presented as stale:
// the autonomy indicator dims/desaturates and its last-updated label is marked,
// mirroring the System Health card and per-project status-badge stale pattern so
// a frozen reading (backgrounded tab, unreachable backend, swallowed error) is
// never shown as a live one. ~3x DEFAULT_POLL_MS (30s) leaves room for one
// missed tick before degrading.
const STATUS_STALE_AFTER_SECONDS = 90;

// How often the component re-renders purely to re-evaluate staleness. A stalled
// poll loop produces no re-render on its own, so without this tick the stale
// flag would never flip until the next successful fetch. Clears naturally when
// fetchSummary sets a fresh lastUpdated.
const STALE_TICK_MS = 15_000;

// Tailwind classes applied when the reading is stale — desaturate + dim (same
// affordance as SystemHealthCard / projectStatus.ts STALE_CLASSES).
const STALE_CLASSES = 'opacity-60 saturate-50';

interface ProjectAutonomousStatus {
  project_id: string;
  project_name: string;
  autonomous_enabled: boolean;
  consecutive_failures: number;
  last_research_at: string | null;
  tasks_spawned_last_hour: number;
  rate_limit: number;
  fleet_default_rate_limit?: number;
  rate_limit_source?: 'project_override' | 'fleet_default';
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

interface StrategicBlockingGuard {
  guard: string;
  reason: string;
  failures?: number;
  decay_at?: string;
  size?: number;
  skipped_at?: string;
  top_contributors?: { key: string; size: number }[];
}

interface PayloadBloatSkip {
  skipped_at?: string;
  size?: number;
  top_contributors?: { key: string; size: number }[];
}

interface ProblemModelTrim {
  status: 'full' | 'summarized' | 'floor' | 'dropped';
  budget_total?: number | null;
  budget_used?: number | null;
  gap_memory_trimmed_count?: number;
  floor?: {
    floor_bytes?: number | null;
    stripped_gap_fields?: string[];
    dropped_gaps?: number;
    summarized_capabilities_count?: number;
    summarized_friction_count?: number;
    still_over_floor?: boolean;
  };
  dropped_reason?: string;
  task_status?: string;
  assembled_at?: string | null;
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
  phase: number;
  blocking_guards: StrategicBlockingGuard[];
  payload_bloat_skip?: PayloadBloatSkip | null;
  problem_model_trim?: ProblemModelTrim | null;
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
  architecture: {
    revision: number;
    last_updated: string | null;
    updated_by_task: string | null;
    system_count: number;
    capability_count: number;
    next_arch_in_cycles: number | null;
  };
  active_task: {
    id: string;
    phase: string;
    status: string;
    started_at: string | null;
  } | null;
  last_cycle: StrategicCycleEntry | null;
  recent_cycles: StrategicCycleEntry[];
}

interface AutonomousStatusProps {
  onRefreshTasks?: () => void;
  wsRef?: RefObject<WebSocket | null>;
  pauseReason?: string | null;
  projectRoles?: Readonly<Record<string, ProjectRole>>;
}

// Four-phase lifecycle: Idle (between ticks), Checking (evaluating), Running
// (research task in flight), Waiting (blocked on something the user should see).
// Spawning is collapsed into Running — it's transient and visually identical.
// "Waiting" replaces "Cooldowns" since not all blockers are cooldowns (VMs full,
// pipeline paused, rate limited, circuit breaker, etc.).
const LIFECYCLE_STEPS = ['Idle', 'Checking', 'Running', 'Waiting'] as const;

function getLifecyclePhase(debug: AutonomyDebug): number {
  const guards = debug.blocking_guards.map(g => g.guard);
  // Autonomy's OWN research is running — genuine progress.
  if (guards.includes('research_task_active')) return 2; // Running
  // Any other blocker: autonomy is waiting on something external. Show as
  // Waiting so the user can read the guard reason to learn why.
  if (guards.some(g => g !== 'waiting_for_interval')) return 3; // Waiting
  if (guards.includes('waiting_for_interval')) return 0; // Idle — between checks
  if (!debug.spawn_blocked) return 1; // Checking — evaluating spawn eligibility now
  return 0;
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
    <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded font-mono ${styles[state] ?? 'bg-neutral-700 text-neutral-400'}`}>
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
      <p className="text-ui-sm font-semibold text-red-400 uppercase tracking-wider mb-1.5">
        Why autonomy isn't spawning
      </p>
      <ul className="space-y-1">
        {errorGuards.map((g) => (
          <li key={g.guard} className="flex items-start gap-1.5">
            <span className="mt-px"><GuardIcon guard={g.guard} /></span>
            <span className={`text-ui-md leading-tight ${guardColor(g.guard)}`}>
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
    <div className="flex items-center justify-between text-ui-md">
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
          <span className="text-red-400 font-mono text-ui-sm">-{formatSeconds(secsLeft)}</span>
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
      <p className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider mb-1.5 flex items-center gap-1">
        <Zap className="w-3 h-3" />Tactical (research)
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
                  className={`text-ui-xs mt-0.5 leading-tight text-center whitespace-nowrap ${
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

function phaseLabelForStrategicTask(phase: string): string {
  if (phase === 'design_doc_update') return 'Architecture review';
  if (phase === 'strategic_review') return 'Strategic review';
  return phase;
}

function strategicOutcomeBadge(outcome: string | undefined) {
  if (outcome === 'success' || outcome === 'completed') {
    return <span className="inline-flex items-center gap-1 text-emerald-400 text-ui-sm"><CheckCircle2 className="w-3 h-3" />success</span>;
  }
  if (outcome === 'in_progress') {
    return <span className="inline-flex items-center gap-1 text-cyan-400 text-ui-sm"><Activity className="w-3 h-3" />in progress</span>;
  }
  if (outcome) {
    return <span className="inline-flex items-center gap-1 text-red-400 text-ui-sm"><XCircle className="w-3 h-3" />{outcome}</span>;
  }
  return null;
}

function StrategicGuardLabel(g: StrategicBlockingGuard): string {
  if (g.guard === 'strategic_failures_high') {
    const decaySecs = g.decay_at ? secondsUntil(g.decay_at) : null;
    const decayStr = decaySecs && decaySecs > 0 ? ` — auto-recovers in ${formatSeconds(decaySecs)}` : '';
    return `${g.failures ?? '?'} consecutive strategic failures${decayStr}`;
  }
  return g.reason;
}

function StrategicLifecycleIndicator({ debug }: { debug: StrategicDebug }) {
  const activeStep = debug.phase;
  const subLabel = (() => {
    if (activeStep === 2 && debug.active_task) return phaseLabelForStrategicTask(debug.active_task.phase);
    if ((activeStep === 0 || activeStep === 1) && debug.next_strategic_in != null) {
      const spawns = debug.next_strategic_in;
      const kind = debug.next_is_architecture ? 'arch' : 'strategic';
      return spawns === 0 ? `${kind} now` : `${kind} in ${spawns}`;
    }
    return null;
  })();
  return (
    <div className="mt-2">
      <p className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider mb-1.5 flex items-center gap-1">
        <Compass className="w-3 h-3" />Strategic (directions)
      </p>
      <div className="flex items-center w-full">
        {LIFECYCLE_STEPS.map((step, i) => {
          const isActive = i === activeStep;
          const isPast = i < activeStep;
          return (
            <div key={step} className="flex items-center flex-1 min-w-0">
              <div className="flex flex-col items-center shrink-0">
                <div className={`w-2 h-2 rounded-full border transition-colors ${
                  isActive ? 'bg-cyan-400 border-cyan-400 ring-2 ring-cyan-400/30'
                  : isPast ? 'bg-neutral-600 border-neutral-600' : 'bg-transparent border-neutral-600'
                }`} />
                <span className={`text-ui-xs mt-0.5 leading-tight text-center whitespace-nowrap ${
                  isActive ? 'text-cyan-400 font-semibold' : 'text-neutral-600'
                }`}>
                  {step}
                  {isActive && subLabel && <span className="block text-neutral-600 font-normal">{subLabel}</span>}
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

function formatTimeAgoShort(iso: string | null): string {
  if (!iso) return 'never';
  const diff = Date.now() - parseTimestamp(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function PayloadBloatIndicator({ skip }: { skip: PayloadBloatSkip }) {
  const sizeKb = skip.size != null ? Math.round(skip.size / 1024) : null;
  const contributors = (skip.top_contributors || []).slice(0, 3);
  return (
    <div className="rounded-md bg-amber-500/10 border border-amber-500/40 px-2.5 py-2">
      <div className="flex items-center justify-between">
        <span className="text-ui-sm font-semibold text-amber-400 uppercase tracking-wider flex items-center gap-1">
          <AlertTriangle className="w-3 h-3 shrink-0" />Strategic paused — payload too large
        </span>
        {skip.skipped_at && (
          <span className="font-mono text-ui-sm text-amber-400/70">{formatTimeAgoShort(skip.skipped_at)}</span>
        )}
      </div>
      <p className="text-ui-md leading-tight text-amber-300 mt-1">
        Assembled payload{sizeKb != null ? ` (${sizeKb} KB)` : ''} exceeds the {MAX_PAYLOAD_KB} KB cap, so strategic refresh is frozen.
        Compact{' '}
        {contributors.length > 0
          ? contributors.map((c) => c.key).join(', ')
          : 'design_doc / problem_model'}
        {' '}rather than hunting for a stuck task.
      </p>
    </div>
  );
}

const TRIM_STATUS_META: Record<ProblemModelTrim['status'], { label: string; tone: string }> = {
  full: { label: 'shipped in full', tone: 'text-emerald-400' },
  summarized: { label: 'summarized to fit budget', tone: 'text-amber-400' },
  floor: { label: 'degraded to read-time floor', tone: 'text-amber-400' },
  dropped: { label: 'dropped from payload', tone: 'text-red-400' },
};

function ProblemModelTrimIndicator({ trim }: { trim: ProblemModelTrim }) {
  const meta = TRIM_STATUS_META[trim.status] ?? TRIM_STATUS_META.full;
  // "full" is the healthy case — show a compact one-liner; trimmed states get a
  // callout box explaining what the strategist actually saw.
  if (trim.status === 'full') {
    const trimmedGaps = trim.gap_memory_trimmed_count ?? 0;
    return (
      <div className="flex items-center justify-between text-ui-md text-neutral-500">
        <span className="flex items-center gap-1">
          <FileText className="w-3 h-3 shrink-0" />Problem model
        </span>
        <span className="font-mono text-ui-sm text-emerald-400">
          full{trimmedGaps > 0 ? ` · ${trimmedGaps} gap${trimmedGaps === 1 ? '' : 's'} trimmed` : ''}
        </span>
      </div>
    );
  }
  const floor = trim.floor;
  const droppedGaps = floor?.dropped_gaps ?? 0;
  const summarizedCapabilities = floor?.summarized_capabilities_count ?? 0;
  const summarizedFriction = floor?.summarized_friction_count ?? 0;
  const skeletonizedSummaryParts: string[] = [];
  if (summarizedCapabilities > 0) {
    skeletonizedSummaryParts.push(
      `${summarizedCapabilities} ${summarizedCapabilities === 1 ? 'capability' : 'capabilities'}`,
    );
  }
  if (summarizedFriction > 0) {
    skeletonizedSummaryParts.push(
      `${summarizedFriction} ${summarizedFriction === 1 ? 'friction item' : 'friction items'}`,
    );
  }
  const stripped = floor?.stripped_gap_fields ?? [];
  return (
    <div className="rounded-md bg-amber-500/10 border border-amber-500/30 px-2.5 py-2">
      <div className="flex items-center justify-between">
        <span className={`text-ui-sm font-semibold uppercase tracking-wider flex items-center gap-1 ${meta.tone}`}>
          <AlertTriangle className="w-3 h-3 shrink-0" />Problem model {meta.label}
        </span>
        {trim.assembled_at && (
          <span className="font-mono text-ui-sm text-amber-400/70">{formatTimeAgoShort(trim.assembled_at)}</span>
        )}
      </div>
      <p className="text-ui-md leading-tight text-amber-300 mt-1">
        Read-time packing trimmed the problem_model, so the strategist saw a reduced view.
        {trim.status === 'dropped' && (trim.dropped_reason ? ` ${trim.dropped_reason}.` : ' It was omitted entirely.')}
        {trim.status === 'floor' && droppedGaps > 0 && ` Dropped ${droppedGaps} low-priority gap${droppedGaps === 1 ? '' : 's'}.`}
        {trim.status === 'floor' && stripped.length > 0 && ` Stripped per-gap ${stripped.join(', ')}.`}
        {trim.status === 'floor' && skeletonizedSummaryParts.length > 0 && ` Skeletonized ${skeletonizedSummaryParts.join(' and ')}.`}
        {floor?.still_over_floor && ' Still over the floor after trimming.'}
      </p>
    </div>
  );
}

function StrategicSection({ strategic }: { strategic: StrategicDebug }) {
  const bloat = strategic.payload_bloat_skip;
  const trim = strategic.problem_model_trim;
  const otherGuards = strategic.blocking_guards.filter((g) => g.guard !== 'payload_bloat_skip');
  return (
    <div className="mt-3 pt-3 border-t border-neutral-800 space-y-1.5">
      {/* Payload-bloat stall takes precedence over the green countdown */}
      {bloat && <PayloadBloatIndicator skip={bloat} />}

      {/* Last read-time packing verdict for the problem_model the strategist saw */}
      {trim && <ProblemModelTrimIndicator trim={trim} />}

      {/* Active task or next countdown */}
      {strategic.active_task ? (
        <div className="flex items-center justify-between text-ui-md">
          <span className="text-neutral-500 flex items-center gap-1">
            <Activity className="w-3 h-3 shrink-0" />{phaseLabelForStrategicTask(strategic.active_task.phase)}
          </span>
          <span className="text-cyan-400 font-mono text-ui-sm">{strategic.active_task.status}</span>
        </div>
      ) : bloat ? null : strategic.next_strategic_in != null && (
        <div className="flex items-center justify-between text-ui-md text-neutral-500">
          <span>Next strategic cycle</span>
          <span className="font-mono">
            {strategic.next_strategic_in === 0 ? 'ready now' : `${strategic.next_strategic_in} spawn${strategic.next_strategic_in === 1 ? '' : 's'}`}
            {strategic.next_is_architecture && <span className="text-amber-400 ml-1">(arch)</span>}
          </span>
        </div>
      )}

      {/* Cycle counts */}
      <div className="flex items-center justify-between text-ui-md text-neutral-500">
        <span>Spawns · cycles</span>
        <span className="font-mono">{strategic.research_spawn_count} · {strategic.strategic_cycle_count}</span>
      </div>

      {/* Last cycle */}
      {strategic.last_cycle && (
        <div className="flex items-center justify-between text-ui-md text-neutral-500">
          <span>Last cycle</span>
          <span className="flex items-center gap-1.5 font-mono text-ui-sm">
            {strategicOutcomeBadge(strategic.last_cycle.outcome)}
            {strategic.last_cycle.phase2_completed_at && (
              <span className="text-neutral-600">{formatTimeAgoShort(strategic.last_cycle.phase2_completed_at)}</span>
            )}
          </span>
        </div>
      )}

      {!strategic.has_purpose && (
        <div className="rounded-md bg-amber-500/5 border border-amber-500/20 px-2 py-1 flex items-start gap-1.5">
          <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400 mt-0.5" />
          <span className="text-ui-md leading-tight text-amber-300">
            No purpose set (problem model or legacy vision) — strategic cycles will bootstrap once, then stall on failure.
          </span>
        </div>
      )}

      {otherGuards.length > 0 && (
        <div className="rounded-md bg-red-500/5 border border-red-500/20 px-2.5 py-2">
          <p className="text-ui-sm font-semibold text-red-400 uppercase tracking-wider mb-1.5">Why strategic is blocked</p>
          <ul className="space-y-1">
            {otherGuards.map((g) => (
              <li key={g.guard} className="flex items-start gap-1.5">
                <AlertTriangle className="w-3 h-3 shrink-0 text-red-400 mt-px" />
                <span className="text-ui-md leading-tight text-red-300">{StrategicGuardLabel(g)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <StrategicLifecycleIndicator debug={strategic} />
    </div>
  );
}

function ArchitectureSection({ strategic }: { strategic: StrategicDebug }) {
  const arch = strategic.architecture;
  const cyclesUntilArch = arch.next_arch_in_cycles;
  return (
    <div className="mt-3 pt-3 border-t border-neutral-800 space-y-1.5">
      <p className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider flex items-center gap-1">
        <FileText className="w-3 h-3" />Architecture (design doc)
      </p>
      <div className="flex items-center justify-between text-ui-md text-neutral-500">
        <span>Last update</span>
        <span className="font-mono text-ui-sm">
          {formatTimeAgoShort(arch.last_updated)}
          {arch.revision > 0 && <span className="text-neutral-600 ml-1">rev {arch.revision}</span>}
        </span>
      </div>
      <div className="flex items-center justify-between text-ui-md text-neutral-500">
        <span>Systems · capabilities</span>
        <span className="font-mono text-ui-sm">{arch.system_count} · {arch.capability_count}</span>
      </div>
      <div className="flex items-center justify-between text-ui-md text-neutral-500">
        <span>Next review</span>
        <span className="font-mono text-ui-sm">
          {cyclesUntilArch == null ? 'disabled'
            : cyclesUntilArch === 0 ? 'next strategic cycle'
            : `${cyclesUntilArch} cycle${cyclesUntilArch === 1 ? '' : 's'} away`}
        </span>
      </div>
    </div>
  );
}

export function AutonomousStatus({ onRefreshTasks, wsRef, pauseReason, projectRoles = {} }: AutonomousStatusProps) {
  const [projects, setProjects] = useState<ProjectAutonomousStatus[]>([]);
  const [debugData, setDebugData] = useState<Record<string, AutonomyDebug>>({});
  const [strategicData, setStrategicData] = useState<Record<string, StrategicDebug>>({});
  const [expanded, setExpanded] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [resettingBreaker, setResettingBreaker] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // Bumped on a slow interval so staleness is re-evaluated even when the poll
  // loop has stalled (no fetch -> no re-render of its own).
  const [, setStaleTick] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  // Ref so fetchDebug can access the current project list without becoming a
  // dep of the poller (avoids re-subscribing on every projects state update).
  const projectsRef = useRef<ProjectAutonomousStatus[]>([]);
  // Ref so the WS handler can check whether the panel is open without
  // capturing a stale closure over `expanded`.
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;

  // Lightweight summary fetch — always runs at the default cadence.
  const fetchSummary = useCallback(async () => {
    try {
      const headers = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/autonomous-status`, {
        headers: { Authorization: headers.Authorization },
      });
      if (!res.ok) return;
      const data = await res.json();
      const fetched: ProjectAutonomousStatus[] = data.projects || [];
      setProjects(fetched);
      projectsRef.current = fetched;
      setLastUpdated(new Date());
    } catch {
      // silent
    }
  }, []);

  // Per-project debug fetch — only runs when the dropdown is expanded (N×2
  // HTTP calls per tick, one autonomy-debug + one strategic-debug per project).
  const fetchDebug = useCallback(async () => {
    const fetched = projectsRef.current;
    if (fetched.length === 0) return;
    try {
      const settled = await Promise.allSettled(
        fetched.flatMap(p => [
          apiFetch(`/api/admin/projects/${p.project_id}/autonomy-debug`)
            .then(r => (r.ok ? r.json() : null))
            .then((d): ['autonomy', string, AutonomyDebug] | null => (d ? ['autonomy', p.project_id, d as AutonomyDebug] : null))
            .catch(() => null),
          apiFetch(`/api/admin/projects/${p.project_id}/strategic-debug`)
            .then(r => (r.ok ? r.json() : null))
            .then((d): ['strategic', string, StrategicDebug] | null => (d ? ['strategic', p.project_id, d as StrategicDebug] : null))
            .catch(() => null),
        ])
      );
      const newAutonomy: Record<string, AutonomyDebug> = {};
      const newStrategic: Record<string, StrategicDebug> = {};
      for (const result of settled) {
        if (result.status !== 'fulfilled' || !result.value) continue;
        const [kind, id, d] = result.value;
        if (kind === 'autonomy') newAutonomy[id] = d as AutonomyDebug;
        else newStrategic[id] = d as StrategicDebug;
      }
      setDebugData(newAutonomy);
      setStrategicData(newStrategic);
    } catch {
      // silent
    }
  }, []);

  // Summary always polls at the default cadence regardless of panel state.
  usePoller(fetchSummary, { intervalMs: DEFAULT_POLL_MS, deps: [fetchSummary] });
  // Debug data is only fetched when the panel is open; the leading call fires
  // immediately on expand so the operator sees data without waiting for the
  // next 60s tick.
  usePoller(fetchDebug, { intervalMs: 60_000, enabled: expanded, deps: [fetchDebug, expanded] });

  // Keep the staleness flag honest even when the poll loop has gone quiet: a
  // stalled/paused poll produces no re-render, so without this the indicator
  // would look live indefinitely. Cleared implicitly on the next fresh fetch.
  useEffect(() => {
    const id = setInterval(() => setStaleTick(t => t + 1), STALE_TICK_MS);
    return () => clearInterval(id);
  }, []);

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
          fetchSummary();
          if (expandedRef.current) fetchDebug();
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
  }, [wsRef, fetchSummary, fetchDebug]);

  const resetCircuitBreaker = async (projectId: string) => {
    setResettingBreaker(projectId);
    setErrors(e => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { [projectId]: _, ...rest } = e;
      return rest;
    });
    try {
      const res = await apiFetch(`/api/admin/projects/${projectId}/circuit-breaker/reset`, { method: 'POST' });
      if (!res.ok) {
        setErrors(e => ({ ...e, [projectId]: 'Failed to reset circuit breaker' }));
        return;
      }
      await fetchSummary();
      await fetchDebug();
    } catch {
      setErrors(e => ({ ...e, [projectId]: 'Failed to reset circuit breaker' }));
    } finally {
      setResettingBreaker(null);
    }
  };

  const toggleAutonomy = async (projectId: string, enabled: boolean) => {
    setToggling(projectId);
    setErrors(e => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { [projectId]: _, ...rest } = e;
      return rest;
    });
    try {
      const headers = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}/autonomous`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        setErrors(e => ({ ...e, [projectId]: 'Failed to toggle autonomy' }));
        return;
      }
      await fetchSummary();
      onRefreshTasks?.();
    } catch {
      setErrors(e => ({ ...e, [projectId]: 'Failed to toggle autonomy' }));
    } finally {
      setToggling(null);
    }
  };

  const anyEnabled = projects.some(p => p.autonomous_enabled);
  const anyCircuitBreaker = projects.some(p => p.consecutive_failures >= 2);

  // The displayed reading goes stale once the last successful poll is older than
  // the threshold. Reuse the shared freshness helper (same as SystemHealthCard)
  // by deriving its args from lastUpdated; null until the first successful poll,
  // so a never-yet-loaded indicator is not marked stale.
  const ageSeconds = lastUpdated ? (Date.now() - lastUpdated.getTime()) / 1000 : null;
  const { stale } = snapshotFreshness(
    lastUpdated ? lastUpdated.toISOString() : null,
    ageSeconds,
    STATUS_STALE_AFTER_SECONDS,
  );

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
        <span className="text-ui-sm text-neutral-500 font-semibold uppercase tracking-wider mb-1">Autonomy</span>
        <span
          className={`text-xl font-bold font-mono leading-none flex items-center gap-1.5 ${stale ? STALE_CLASSES : ''}`}
          title={stale ? 'Last autonomy reading is stale — poll may have stalled' : undefined}
        >
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
        // eslint-disable-next-line viewport/no-fixed-width -- dropdown drawer; max-w-[calc(100vw-1rem)] caps mobile width
        <div className="absolute top-full mt-2 right-0 w-96 max-w-[calc(100vw-1rem)] bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[70vh] overflow-y-auto custom-scrollbar">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-ui-md font-semibold text-neutral-400 uppercase tracking-wider">Autonomous Pipeline — Tactical · Strategic · Architecture</h3>
            <span className="flex items-center gap-1.5">
              {lastUpdated && (
                <span
                  className={`text-ui-sm font-mono ${stale ? 'text-amber-400 font-semibold' : 'text-neutral-600'}`}
                  title={stale ? 'Last successful update is stale — poll may have stalled' : 'Last successful update'}
                >
                  {stale && '⚠ '}
                  {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                  {stale && ' · stale'}
                </span>
              )}
              <button onClick={() => { fetchSummary(); fetchDebug(); }} className="text-neutral-500 hover:text-neutral-300 transition-colors">
                <RefreshCw className="w-3 h-3" />
              </button>
            </span>
          </div>

          {pauseReason && (
            <div className="mb-3 rounded-md bg-amber-500/10 border border-amber-500/25 px-2.5 py-2 flex items-start gap-1.5">
              <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400 mt-0.5" />
              <span className="text-ui-md text-amber-300 leading-tight">{pauseReason}</span>
            </div>
          )}

          {projects.length === 0 && (
            <p className="text-xs text-neutral-600">No projects found</p>
          )}

          <div className={stale ? STALE_CLASSES : undefined}>
            {projects.map(p => {
              const debug = debugData[p.project_id];
              const strategic = strategicData[p.project_id];
              const access = resolveAccess(undefined, projectRoles[p.project_id]);
              return (
              <div key={p.project_id} className="mb-3 last:mb-0 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
                {/* Header */}
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-neutral-200 truncate max-w-[200px]">{p.project_name}</span>
                  {access.canManageProject ? (
                    /* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */
                    <button
                      onClick={() => toggleAutonomy(p.project_id, !p.autonomous_enabled)}
                      disabled={toggling === p.project_id}
                      className={`text-ui-sm font-semibold uppercase tracking-wider px-2 py-0.5 rounded transition-colors ${
                        p.autonomous_enabled
                          ? 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'
                          : 'bg-neutral-800 text-neutral-500 hover:bg-neutral-700'
                      } ${toggling === p.project_id ? 'opacity-50' : ''}`}
                    >
                      {p.autonomous_enabled ? 'Enabled' : 'Disabled'}
                    </button>
                  ) : (
                    <span className={`text-ui-sm font-semibold uppercase tracking-wider px-2 py-0.5 rounded ${
                      p.autonomous_enabled
                        ? 'bg-emerald-500/10 text-emerald-400'
                        : 'bg-neutral-800 text-neutral-500'
                    }`}>
                      {p.autonomous_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  )}
                </div>
                {errors[p.project_id] && (
                  <div className="mb-2 rounded-md bg-red-500/10 border border-red-500/30 px-2 py-1 flex items-center gap-1.5">
                    <AlertTriangle className="w-3 h-3 shrink-0 text-red-400" />
                    <span className="text-ui-sm text-red-300">{errors[p.project_id]}</span>
                  </div>
                )}

                <div className="space-y-1.5">
                  {/* Circuit breaker — enhanced with state pill when debug available */}
                  {debug ? (
                    <>
                      <CircuitBreakerDetail cb={debug.circuit_breaker} />
                      {access.canOperateProject && (debug.circuit_breaker.state === 'OPEN' || debug.circuit_breaker.state === 'HALF_OPEN') && (
                        <div className="flex justify-end">
                          {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                          <button
                            onClick={() => resetCircuitBreaker(p.project_id)}
                            disabled={resettingBreaker === p.project_id}
                            className={`text-ui-sm font-semibold uppercase tracking-wider px-2 py-0.5 rounded transition-colors bg-red-500/10 text-red-400 hover:bg-red-500/20 ${resettingBreaker === p.project_id ? 'opacity-50' : ''}`}
                          >
                            {resettingBreaker === p.project_id ? 'Resetting…' : 'Reset Circuit Breaker'}
                          </button>
                        </div>
                      )}
                    </>
                  ) : p.consecutive_failures > 0 ? (
                    <div className="flex items-center gap-1.5 text-ui-md">
                      <AlertTriangle className={`w-3 h-3 shrink-0 ${p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}`} />
                      <span className={p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}>
                        {p.consecutive_failures} consecutive failure{p.consecutive_failures !== 1 ? 's' : ''}
                        {p.consecutive_failures >= 3 && ' — circuit breaker tripped'}
                      </span>
                    </div>
                  ) : null}

                  {/* Last research */}
                  <div className="flex items-center justify-between text-ui-md text-neutral-500">
                    <span>Last research</span>
                    <span className="font-mono">{p.last_research_at ? formatTimeAgo(p.last_research_at) : 'never'}</span>
                  </div>

                  {/* Rate limit usage */}
                  <div className="flex items-center justify-between text-ui-md text-neutral-500">
                    <span>Tasks this hour</span>
                    <span className="font-mono flex items-center gap-1">
                      <span className={p.tasks_spawned_last_hour >= p.rate_limit ? 'text-amber-400' : ''}>
                        {p.tasks_spawned_last_hour}
                      </span>
                      /{p.rate_limit}
                      {p.rate_limit_source === 'project_override' && (
                        <span
                          className="ml-1 px-1 rounded text-ui-xs bg-neutral-700 text-neutral-300"
                          title={`Project override (fleet default: ${p.fleet_default_rate_limit ?? '?'})`}
                        >
                          override
                        </span>
                      )}
                    </span>
                  </div>
                </div>

                {/* Tactical debug: spawn guards + research lifecycle */}
                {debug && (
                  <>
                    {debug.spawn_blocked && (
                      <SpawnGuardDiagnostics guards={debug.blocking_guards} />
                    )}
                    <LifecycleIndicator debug={debug} />
                  </>
                )}

                {/* Strategic cycle section */}
                {strategic && <StrategicSection strategic={strategic} />}

                {/* Architecture / design doc section */}
                {strategic && <ArchitectureSection strategic={strategic} />}
              </div>
            );
          })}
          </div>
        </div>
      )}
    </div>
  );
}
