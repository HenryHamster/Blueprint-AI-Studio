import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
import { Compass, Cpu, Copy, Check, RefreshCw, Trash2 } from 'lucide-react';
import { listFleetRunners, registerFleetRunner, revokeFleetRunner } from '../api';
import type { FleetRunner, FleetRunnerRegistrationResult } from '../api';
import { timeAgo, parseTimestamp, formatAbsolute, formatDurationSec } from '../utils/time';
import {
  formatBytes,
  runnerSetupSnippets,
  type RunnerSetupPreset,
  type RunnerSetupSnippetCommands,
} from './FleetRunnersPanel.utils';

const LIVENESS_THRESHOLD_SEC = 300;
const STALE_THRESHOLD_SEC = 600;

type CopyTarget = 'token' | keyof RunnerSetupSnippetCommands;

function fallbackLivenessLabel(lastSeenAt: string | null): { label: string; cls: string } {
  if (!lastSeenAt) return { label: 'never seen', cls: 'text-neutral-500' };
  const ageMs = Date.now() - parseTimestamp(lastSeenAt).getTime();
  const ageSec = ageMs / 1000;
  if (ageSec < LIVENESS_THRESHOLD_SEC) return { label: 'live', cls: 'text-emerald-400' };
  if (ageSec < STALE_THRESHOLD_SEC) return { label: 'stale', cls: 'text-amber-400' };
  return { label: 'offline', cls: 'text-neutral-500' };
}

function deviceStatusLabel(status: string): { label: string; cls: string } {
  if (status === 'connected') return { label: 'connected', cls: 'text-emerald-400' };
  // "degraded" = live but its jobs are failing — amber, distinct from healthy green.
  if (status === 'degraded') return { label: 'failing', cls: 'text-amber-400' };
  if (status === 'stale') return { label: 'stale', cls: 'text-amber-400' };
  if (status === 'offline') return { label: 'offline', cls: 'text-neutral-500' };
  return { label: status.replace(/_/g, ' '), cls: 'text-neutral-500' };
}

/** True when the runner is live (recent heartbeat) but its jobs are failing —
 *  the "connected but failing" false-green the trust telemetry exists to catch. */
function isRunnerDegraded(runner: FleetRunner): boolean {
  return runner.health?.degraded === true || runner.health?.device_status === 'degraded';
}

function livenessLabel(runner: FleetRunner): { label: string; cls: string } {
  const health = runner.health;
  if (health?.device_status) return deviceStatusLabel(health.device_status);
  if (runner.status !== 'active') return { label: 'offline', cls: 'text-neutral-500' };
  if (!health) return fallbackLivenessLabel(runner.last_seen_at);
  if (health.is_live) return { label: 'live', cls: 'text-emerald-400' };
  if (health.stale_reason === 'heartbeat_stale') return { label: 'stale', cls: 'text-amber-400' };
  if (health.stale_reason === 'never_seen') return { label: 'never seen', cls: 'text-neutral-500' };
  return { label: 'offline', cls: 'text-neutral-500' };
}

function isRunnerLive(runner: FleetRunner): boolean {
  const deviceStatus = runner.health?.device_status;
  // A degraded runner is still connected/live (it heartbeats) — it just fails its
  // jobs. Keep it in the live set so capacity accounting is unaffected; readiness
  // (below) is what flags it as not-ready.
  if (deviceStatus) return deviceStatus === 'connected' || deviceStatus === 'degraded';
  if (runner.status !== 'active') return false;
  if (runner.health) return runner.health.is_live;
  return fallbackLivenessLabel(runner.last_seen_at).label === 'live';
}

/** Count of tasks the runner reported executing in its last heartbeat. */
function activeTaskCount(runner: FleetRunner): number {
  if (runner.health) return runner.health.active_task_count;
  const ids = runner.capabilities.active_task_ids;
  return Array.isArray(ids) ? ids.length : 0;
}

function activeWorkCount(runner: FleetRunner): number {
  if (runner.health?.active_work_count !== undefined) return runner.health.active_work_count;
  if (runner.health?.active_compute_job_count !== undefined) {
    return activeTaskCount(runner) + runner.health.active_compute_job_count;
  }
  const activeCompute = Array.isArray(runner.capabilities.active_compute_job_ids)
    ? runner.capabilities.active_compute_job_ids.length
    : 0;
  return activeTaskCount(runner) + activeCompute;
}

function availableSlots(runner: FleetRunner): number | null {
  if (runner.health) {
    const health = runner.health as { available_slots?: number | null; max_concurrent?: number | null };
    if (typeof health.available_slots === 'number') return health.available_slots;
    if (health.available_slots === null) return null;
    if (typeof health.max_concurrent === 'number') {
      return Math.max(0, health.max_concurrent - activeWorkCount(runner));
    }
    return null;
  }
  const max = runner.capabilities.max_concurrent;
  if (typeof max !== 'number') return null;
  return Math.max(0, max - activeWorkCount(runner));
}

function maxSlotsForRunner(runner: FleetRunner): number | null {
  const max = runner.health?.max_concurrent ?? runner.capabilities.max_concurrent;
  return typeof max === 'number' ? max : null;
}

function isComputeCapable(runner: FleetRunner): boolean {
  const taskTypes = runner.capabilities.task_types;
  return taskTypes === 'all' || (Array.isArray(taskTypes) && taskTypes.includes('compute_experiment'));
}

function capsSummary(caps: FleetRunner['capabilities']): string {
  const parts: string[] = [];
  if (caps.task_types) {
    if (caps.task_types === 'all') {
      parts.push('all task types');
    } else {
      parts.push(Array.isArray(caps.task_types) ? caps.task_types.join(', ') : caps.task_types);
    }
  }
  if (typeof caps.max_concurrent === 'number') parts.push(`max ${caps.max_concurrent} concurrent`);
  return parts.join(' · ') || '—';
}

function isStrategicRunner(runner: FleetRunner): boolean {
  const taskTypes = runner.capabilities.task_types;
  return runner.capabilities.runner_preset === 'strategic_local'
    || runner.capabilities.workload === 'strategic'
    || taskTypes === 'all'
    || (Array.isArray(taskTypes) && (taskTypes.includes('strategic_review') || taskTypes.includes('design_doc_update')));
}

function heartbeatLabel(runner: FleetRunner): string {
  const secondsSinceLastSeen = runner.health?.seconds_since_last_seen;
  if (typeof secondsSinceLastSeen === 'number' && Number.isFinite(secondsSinceLastSeen)) {
    return `heartbeat ${formatDurationSec(Math.max(0, secondsSinceLastSeen))} ago`;
  }
  if (runner.last_seen_at) return `heartbeat ${timeAgo(runner.last_seen_at)}`;
  return 'no heartbeat yet';
}

/** Relative "last successful job" label — the trust signal that a runner is not
 *  just connected but actually completing work. */
function lastSuccessLabel(runner: FleetRunner): string | null {
  const health = runner.health;
  if (!health) return null;
  const secs = health.seconds_since_last_success;
  if (typeof secs === 'number' && Number.isFinite(secs)) {
    return `last success ${formatDurationSec(Math.max(0, secs))} ago`;
  }
  if (health.last_successful_job_at) return `last success ${timeAgo(health.last_successful_job_at)}`;
  // Health present but no success recorded — only meaningful once telemetry exists.
  if (typeof health.consecutive_job_failures === 'number') return 'no successful job yet';
  return null;
}

/** Trust telemetry one-liner for a runner row: last successful job, GPU util,
 *  disk free. Returns null when the runner reported no telemetry (old runners). */
function telemetrySummary(runner: FleetRunner): { text: string; warn: boolean } | null {
  const health = runner.health;
  const parts: string[] = [];
  const success = lastSuccessLabel(runner);
  if (success) parts.push(success);
  const failures = health?.consecutive_job_failures;
  if (typeof failures === 'number' && failures > 0) parts.push(`${failures} fail streak`);
  const gpuUtil = health?.gpu_utilization_pct ?? runner.capabilities.gpu_utilization_pct;
  if (typeof gpuUtil === 'number') parts.push(`GPU ${gpuUtil}%`);
  const diskFree = health?.disk_free_bytes ?? runner.capabilities.disk_free_bytes;
  if (typeof diskFree === 'number') parts.push(`${formatBytes(diskFree)} disk`);
  if (parts.length === 0) return null;
  return { text: parts.join(' · '), warn: isRunnerDegraded(runner) || health?.disk_low === true };
}

function runnerReadinessDetail(runner: FleetRunner, runnerLive: boolean, slots: number | null): string {
  const heartbeat = heartbeatLabel(runner);
  if (runner.status !== 'active') return `${heartbeat}; inactive`;
  if (!runner.last_seen_at && runner.health?.seconds_since_last_seen == null) {
    return `${heartbeat}; start runner`;
  }
  if (!runnerLive) return `${heartbeat}; waiting for next heartbeat`;
  if (slots === 0) return `${heartbeat}; no slots free`;
  return `${heartbeat}; ready`;
}

type ReadinessTone = 'good' | 'warn' | 'bad' | 'neutral';

interface RunnerReadiness {
  label: string;
  detail: string;
  nextAction: string;
  tone: ReadinessTone;
  ready: boolean;
}

function runnerReadiness(runner: FleetRunner, runnerLive: boolean, slots: number | null): RunnerReadiness {
  const detail = runnerReadinessDetail(runner, runnerLive, slots);
  if (runner.status !== 'active') {
    return { label: 'inactive', detail, nextAction: 'Re-enroll or revoke this runner', tone: 'neutral', ready: false };
  }
  if (!runner.last_seen_at && runner.health?.seconds_since_last_seen == null) {
    return { label: 'not started', detail, nextAction: 'Start the runner process', tone: 'warn', ready: false };
  }
  if (!runnerLive) {
    const reason = runner.health?.device_status_reason ?? runner.health?.stale_reason;
    return {
      label: runner.health?.device_status === 'stale' ? 'heartbeat stale' : 'offline',
      detail: reason ? `${detail}; ${reason.replace(/_/g, ' ')}` : detail,
      nextAction: 'Check host and runner heartbeat',
      tone: 'warn',
      ready: false,
    };
  }
  if (runner.health?.agent_available === false) {
    return { label: 'agent unavailable', detail, nextAction: 'Restart or inspect the local agent', tone: 'bad', ready: false };
  }
  if (isRunnerDegraded(runner)) {
    // The payoff: live + heartbeating, but jobs are failing. Amber, NOT ready —
    // this is the false-green the trust telemetry kills.
    const heartbeat = heartbeatLabel(runner);
    const failures = runner.health?.consecutive_job_failures ?? 0;
    const detailText =
      runner.health?.degraded_reason === 'success_stale'
        ? `${heartbeat}; no recent successful job`
        : `${heartbeat}; ${failures} consecutive job failure${failures === 1 ? '' : 's'}`;
    return {
      label: 'connected but failing',
      detail: detailText,
      nextAction: 'Inspect the runner host — jobs are failing despite a healthy heartbeat',
      tone: 'warn',
      ready: false,
    };
  }
  if (slots == null) {
    return { label: 'slots unknown', detail, nextAction: 'Confirm runner slot telemetry', tone: 'warn', ready: false };
  }
  if (slots === 0) {
    return { label: 'full', detail, nextAction: 'Wait for a slot or add capacity', tone: 'warn', ready: false };
  }
  return { label: 'ready', detail, nextAction: 'No action needed', tone: 'good', ready: true };
}

function readinessClasses(tone: ReadinessTone): string {
  if (tone === 'good') return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
  if (tone === 'warn') return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
  if (tone === 'bad') return 'border-red-500/30 bg-red-500/10 text-red-300';
  return 'border-neutral-700 bg-neutral-950/40 text-neutral-400';
}

interface TokenDialogProps {
  runnerId: string;
  token: string;
  preset?: RunnerSetupPreset;
  tokenExpiresAt?: string | null;
  onDismiss: () => void;
}

function TokenDialog({ runnerId, token, preset = 'default', tokenExpiresAt, onDismiss }: TokenDialogProps) {
  const [copied, setCopied] = useState<Record<CopyTarget, boolean>>({
    token: false,
    windowsPowerShell: false,
    macosLinux: false,
  });
  const origin = typeof window !== 'undefined' ? window.location.origin : 'https://your-orchestrator';
  const snippets = runnerSetupSnippets(token, origin, preset);
  const setupSnippetItems: Array<{ id: keyof RunnerSetupSnippetCommands; label: string; command: string }> = [
    { id: 'windowsPowerShell', label: 'Windows PowerShell', command: snippets.windowsPowerShell },
    { id: 'macosLinux', label: 'macOS/Linux', command: snippets.macosLinux },
  ];

  const copy = async (target: CopyTarget, text: string) => {
    await navigator.clipboard?.writeText(text);
    setCopied(prev => ({ ...prev, [target]: true }));
    window.setTimeout(() => {
      setCopied(prev => ({ ...prev, [target]: false }));
    }, 2000);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" />
      <div className="relative z-10 w-full max-w-2xl bg-neutral-900 border border-neutral-700 rounded-lg p-5 shadow-2xl mx-4">
        <h3 className="text-sm font-semibold text-neutral-200 mb-1">
          {preset === 'strategic_local' ? 'Strategic runner enrolled' : 'Runner enrolled'}
        </h3>
        <p className="text-xs text-amber-400 mb-3 font-medium">
          This token is shown only once and cannot be retrieved again — copy it now.
        </p>

        <div className="mb-3">
          <div className="text-xs text-neutral-400 mb-1">Token</div>
          <div className="flex items-center gap-2 bg-neutral-950 border border-neutral-700 rounded px-3 py-2">
            <code className="flex-1 text-xs text-emerald-300 break-all font-mono">{token}</code>
            <button
              onClick={() => copy('token', token)}
              className="shrink-0 text-neutral-400 hover:text-neutral-200 transition-colors p-1 min-w-[44px] min-h-[44px] flex items-center justify-center"
              title="Copy token"
              aria-label="Copy token"
            >
              {copied.token ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        <div className="mb-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-neutral-400">Setup snippets</div>
            {tokenExpiresAt && (
              <div className="text-ui-md text-neutral-500">Token expires {formatAbsolute(tokenExpiresAt)}</div>
            )}
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {setupSnippetItems.map(item => (
              <div key={item.id} className="min-w-0 rounded border border-neutral-700 bg-neutral-950">
                <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-1.5">
                  <div className="text-ui-md font-semibold uppercase tracking-wide text-neutral-500">{item.label}</div>
                  <button
                    onClick={() => copy(item.id, item.command)}
                    className="flex min-h-[44px] min-w-[44px] items-center justify-center text-neutral-400 transition-colors hover:text-neutral-200 md:min-h-8 md:min-w-8"
                    title={`Copy ${item.label} setup snippet`}
                    aria-label={`Copy ${item.label} setup snippet`}
                  >
                    {copied[item.id] ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                  </button>
                </div>
                <code className="block max-h-28 overflow-auto whitespace-pre-wrap break-all px-3 py-2 font-mono text-xs text-neutral-300">
                  {item.command}
                </code>
              </div>
            ))}
          </div>
          <div className="text-ui-md text-neutral-500">Next: start the runner; first heartbeat marks it live.</div>
        </div>

        <div className="text-xs text-neutral-500 mb-4">Runner ID: {runnerId}</div>

        <button
          onClick={onDismiss}
          className="w-full py-2 text-sm font-medium bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded transition-colors min-h-[44px]"
        >
          I've copied the token — close
        </button>
      </div>
    </div>
  );
}

interface Props {
  projectId: string;
  requireLocalRunner?: boolean;
}

function tokenExpiryFromRegisterResult(result: FleetRunnerRegistrationResult): string | null {
  return result.token_expires_at ?? result.expires_at ?? result.expiresAt ?? null;
}

export function FleetRunnersPanel({ projectId, requireLocalRunner = false }: Props) {
  const [runners, setRunners] = useState<FleetRunner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enrollingPreset, setEnrollingPreset] = useState<RunnerSetupPreset | null>(null);
  const [pendingToken, setPendingToken] = useState<{
    runnerId: string;
    token: string;
    preset: RunnerSetupPreset;
    tokenExpiresAt: string | null;
  } | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await listFleetRunners(projectId);
      if (mountedRef.current) { setRunners(data); setError(null); }
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { setLoading(true); load(); }, [load]);

  useEffect(() => {
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [load]);

  const handleEnroll = async (preset: RunnerSetupPreset = 'default') => {
    setEnrollingPreset(preset);
    try {
      const result = preset === 'strategic_local'
        ? await registerFleetRunner(projectId, { preset: 'strategic_local' })
        : await registerFleetRunner(projectId);
      setPendingToken({
        runnerId: result.runner_id,
        token: result.token,
        preset,
        tokenExpiresAt: tokenExpiryFromRegisterResult(result),
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setEnrollingPreset(null);
    }
  };

  const handleRevoke = async (runner: FleetRunner) => {
    if (!window.confirm(`Revoke runner ${runner.hostname ?? runner.id}? It will no longer be able to authenticate.`)) return;
    setRevoking(runner.id);
    try {
      await revokeFleetRunner(projectId, runner.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRevoking(null);
    }
  };

  const { liveRunners, liveComputeRunners, liveGpuRunners, liveStrategicRunners } = useMemo(() => {
    const live = runners.filter(isRunnerLive);
    return {
      liveRunners: live,
      liveComputeRunners: live.filter(isComputeCapable),
      liveGpuRunners: live.filter(runner => runner.capabilities.has_gpu),
      liveStrategicRunners: live.filter(isStrategicRunner),
    };
  }, [runners]);

  const { readyRunners, eligibleStrategicRunners } = useMemo(() => {
    const readiness = liveRunners.map(runner => runnerReadiness(runner, true, availableSlots(runner)));
    const strategicReadiness = liveStrategicRunners.map(runner => runnerReadiness(runner, true, availableSlots(runner)));
    return {
      readyRunners: readiness.filter(r => r.ready).length,
      eligibleStrategicRunners: strategicReadiness.filter(r => r.ready).length,
    };
  }, [liveRunners, liveStrategicRunners]);

  const { failingRunners, agentUnavailable, strategicAgentUnavailable, busyWork, maxSlots, knownFreeSlots, hasUnknownSlots, freeSlotsLabel, gpuCapacityLabel } = useMemo(() => {
    const slotValues = liveRunners.map(availableSlots);
    const knownFree = slotValues.reduce<number>((sum, slots) => sum + (slots ?? 0), 0);
    const unknownSlots = slotValues.some(slots => slots == null);
    const gpuSlotValues = liveGpuRunners.map(availableSlots);
    const knownGpuSlots = gpuSlotValues.reduce<number>((sum, slots) => sum + (slots ?? 0), 0);
    const unknownGpuSlots = gpuSlotValues.some(slots => slots == null);
    return {
      failingRunners: liveRunners.filter(isRunnerDegraded).length,
      agentUnavailable: liveRunners.filter(runner => runner.health?.agent_available === false).length,
      strategicAgentUnavailable: liveStrategicRunners.filter(runner => runner.health?.agent_available === false).length,
      busyWork: liveRunners.reduce((sum, runner) => sum + activeWorkCount(runner), 0),
      maxSlots: liveRunners.reduce((sum, runner) => sum + (maxSlotsForRunner(runner) ?? 0), 0),
      knownFreeSlots: knownFree,
      hasUnknownSlots: unknownSlots,
      freeSlotsLabel: unknownSlots ? `${knownFree}+ known` : `${knownFree} free`,
      gpuCapacityLabel: liveGpuRunners.length === 0
        ? 'offline'
        : unknownGpuSlots
          ? `${liveGpuRunners.length} connected`
          : `${knownGpuSlots} slots free`,
    };
  }, [liveRunners, liveGpuRunners, liveStrategicRunners]);
  const localOnlyBlocked = requireLocalRunner && !loading && liveComputeRunners.length === 0;
  const nextAction = (() => {
    if (loading) return null;
    if (runners.length === 0) return 'Enroll a runner, then start it to send the first heartbeat.';
    if (localOnlyBlocked) return 'Start or enroll a connected compute-capable local runner.';
    if (agentUnavailable > 0) return 'Inspect agent availability on the affected runner host.';
    if (failingRunners > 0) return 'A connected runner is failing its jobs; inspect the host logs before relying on it.';
    if (liveRunners.length === 0) return 'Check runner hosts; no active runner is connected.';
    if (!hasUnknownSlots && knownFreeSlots === 0) return 'Capacity is full; wait for work to finish or enroll another runner.';
    if (hasUnknownSlots && knownFreeSlots === 0) return 'Confirm runner slot telemetry; connected capacity is not reporting slots.';
    return 'Fleet has dispatch capacity.';
  })();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-300">Self-hosted runners</h3>
          <div className="mt-0.5 truncate text-ui-md text-neutral-500">{projectId}</div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            onClick={load}
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-neutral-800 text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-[#151c25] hover:text-neutral-200 md:min-h-9 md:min-w-9"
            aria-label="Refresh runners"
            title="Refresh"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => handleEnroll('strategic_local')}
            disabled={enrollingPreset !== null}
            className="min-h-[44px] rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 text-xs font-semibold text-emerald-300 transition-colors hover:bg-emerald-500/15 disabled:opacity-50 md:min-h-9"
            title="Enroll a local runner restricted to strategic_review and design_doc_update"
          >
            {enrollingPreset === 'strategic_local' ? 'Enrolling...' : 'Strategic runner'}
          </button>
          <button
            onClick={() => handleEnroll('default')}
            disabled={enrollingPreset !== null}
            className="min-h-[44px] rounded-md border border-neutral-700 bg-[#151c25] px-3 text-xs font-semibold text-neutral-100 transition-colors hover:border-neutral-600 hover:bg-neutral-800 disabled:opacity-50 md:min-h-9"
          >
            {enrollingPreset === 'default' ? 'Enrolling...' : 'Enroll runner'}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-400/20 bg-red-400/10 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {!loading && runners.length > 0 && (
        <div className="grid grid-cols-2 gap-2 text-xs lg:grid-cols-4">
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">readiness</div>
            <div className={`mt-0.5 font-mono ${readyRunners > 0 ? 'text-emerald-300' : 'text-amber-300'}`}>
              {readyRunners}/{runners.length} ready
            </div>
            <div className={`mt-0.5 truncate text-ui-sm ${failingRunners > 0 ? 'text-amber-300' : 'text-neutral-600'}`} data-testid="fleet-runner-connected-summary">
              {failingRunners > 0
                ? `${liveRunners.length} connected · ${failingRunners} failing`
                : `${liveRunners.length} connected`}
            </div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">slots</div>
            <div className="mt-0.5 font-mono text-neutral-100" data-testid="fleet-runner-work-capacity">{busyWork}/{maxSlots || '-'}</div>
            <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{freeSlotsLabel}</div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">gpu</div>
            <div className={`mt-0.5 font-mono ${liveGpuRunners.length > 0 ? 'text-purple-300' : 'text-neutral-500'}`}>
              {gpuCapacityLabel}
            </div>
            <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{liveGpuRunners.length} connected</div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">strategic</div>
            <div className={`mt-0.5 font-mono ${eligibleStrategicRunners > 0 ? 'text-emerald-300' : 'text-neutral-500'}`} data-testid="fleet-runner-strategic-readiness">
              {eligibleStrategicRunners > 0 ? `${eligibleStrategicRunners} eligible live` : 'none eligible'}
            </div>
            <div className={`mt-0.5 truncate text-ui-sm ${strategicAgentUnavailable > 0 ? 'text-red-300' : 'text-neutral-600'}`}>
              {strategicAgentUnavailable > 0
                ? `${liveStrategicRunners.length} connected / ${strategicAgentUnavailable} agent blocked`
                : `${liveStrategicRunners.length} connected`}
            </div>
          </div>
        </div>
      )}

      {nextAction && runners.length > 0 && (
        <div
          className={`rounded-md border px-3 py-2 text-xs ${
            nextAction === 'Fleet has dispatch capacity.'
              ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-200'
              : 'border-amber-500/20 bg-amber-500/10 text-amber-200'
          }`}
          data-testid="fleet-next-action"
        >
          <span className="font-semibold">Next action:</span> {nextAction}
        </div>
      )}

      {localOnlyBlocked && (
        <div className="rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          Local-only compute is enabled for this project. Compute experiments will wait until a connected compute-capable runner is available.
        </div>
      )}

      {loading ? (
        <div className="py-4 text-center text-xs text-neutral-500">Loading…</div>
      ) : runners.length === 0 ? (
        <div className="rounded-md border border-dashed border-neutral-800 py-6 text-center text-xs text-neutral-500">
          No runners enrolled. Click <span className="text-neutral-300">Enroll runner</span> to register one.
        </div>
      ) : (
        <div className="space-y-2">
          {runners.map(runner => {
            const { label: livLabel, cls: livCls } = livenessLabel(runner);
            // status is authoritative (set by the backend stale-runner sweep);
            // distinct from the timestamp-derived live/stale label above.
            const isActive = runner.status === 'active';
            const runnerLive = isRunnerLive(runner);
            const activeCount = activeWorkCount(runner);
            const slots = runnerLive ? availableSlots(runner) : null;
            const strategicRunner = isStrategicRunner(runner);
            const readiness = runnerReadiness(runner, runnerLive, slots);
            const telemetry = telemetrySummary(runner);
            return (
              <div
                key={runner.id}
                className="flex items-start gap-2.5 rounded-md border border-neutral-800 bg-[#0d1218] p-2.5"
                data-testid="fleet-runner-row"
              >
                <div className="mt-0.5 shrink-0">
                  <div className="flex h-7 w-7 items-center justify-center rounded-md border border-neutral-800 bg-[#151c25]">
                    <Cpu className="h-3.5 w-3.5 text-neutral-400" />
                  </div>
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-sm font-medium text-neutral-200 truncate">
                      {runner.hostname ?? runner.id.slice(0, 8)}
                    </span>
                    {runner.version && (
                      <span className="text-xs text-neutral-500">{runner.version}</span>
                    )}
                    <span className={`rounded border border-neutral-700 bg-neutral-950/40 px-1.5 py-0.5 text-xs font-medium ${livCls}`}>{livLabel}</span>
                    {strategicRunner && (
                      <span className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-xs text-emerald-300">
                        <Compass className="w-3 h-3" />
                        Strategic capable
                      </span>
                    )}
                    {!isActive && (
                      <span className="rounded border border-neutral-600/50 bg-neutral-700/40 px-1.5 py-0.5 text-xs text-neutral-300">
                        {runner.status === 'inactive' ? 'offline (deactivated)' : runner.status}
                      </span>
                    )}
                    {runnerLive && activeCount > 0 && (
                      <span className="rounded border border-blue-500/30 bg-blue-500/10 px-1.5 py-0.5 text-xs text-blue-300">
                        {activeCount} active
                      </span>
                    )}
                    {slots !== null && (
                      <span className="rounded border border-neutral-700 bg-[#151c25] px-1.5 py-0.5 text-xs text-neutral-300">
                        {slots} slots free
                      </span>
                    )}
                    {runner.capabilities.has_gpu && (
                      <span className="rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-xs text-purple-300">GPU</span>
                    )}
                    <span className={`rounded border px-1.5 py-0.5 text-xs font-medium ${readinessClasses(readiness.tone)}`}>
                      {readiness.label}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-neutral-500">{capsSummary(runner.capabilities)}</div>
                  {telemetry && (
                    <div
                      className={`mt-0.5 text-ui-md ${telemetry.warn ? 'text-amber-300/90' : 'text-neutral-500'}`}
                      data-testid="fleet-runner-telemetry"
                    >
                      {telemetry.text}
                    </div>
                  )}
                  <div className="mt-0.5 text-ui-md text-neutral-600">{readiness.detail}</div>
                  <div className={`mt-0.5 text-ui-md ${readiness.ready ? 'text-emerald-400/80' : 'text-amber-300/90'}`}>
                    Next: {readiness.nextAction}
                  </div>
                </div>
                <button
                  onClick={() => handleRevoke(runner)}
                  disabled={revoking === runner.id}
                  className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-red-500/10 hover:text-red-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
                  aria-label={`Revoke runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                  title="Revoke runner"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {pendingToken && (
        <TokenDialog
          runnerId={pendingToken.runnerId}
          token={pendingToken.token}
          preset={pendingToken.preset}
          tokenExpiresAt={pendingToken.tokenExpiresAt}
          onDismiss={() => setPendingToken(null)}
        />
      )}
    </div>
  );
}
