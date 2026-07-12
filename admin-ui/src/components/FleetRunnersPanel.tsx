/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
import { Compass, Cpu, Copy, Check, KeyRound, RefreshCw, Trash2, Edit2, PauseCircle, PlayCircle, Wind, Activity } from 'lucide-react';
import { API_BASE_URL, listFleetRunners, patchRunnerStatus, registerFleetRunner, revokeFleetRunner, rotateFleetRunnerToken, updateRunnerCapabilities, fetchRunnerTelemetry, fetchRunnerStats } from '../api';
import type { FleetRunner, FleetRunnerRegistrationResult, RunnerTelemetryResponse, RunnerStatsResponse } from '../api';
import { ConfirmDialog } from './ConfirmDialog';
import { timeAgo, parseTimestamp, formatAbsolute, formatDurationSec } from '../utils/time';
import {
  formatBytes,
  formatBackendInventory,
  runnerSetupSnippets,
  type RunnerSetupPreset,
  type RunnerSetupSnippetCommands,
} from './FleetRunnersPanel.utils';

const LIVENESS_THRESHOLD_SEC = 300;
const STALE_THRESHOLD_SEC = 600;

type CopyTarget = 'token' | keyof RunnerSetupSnippetCommands;
type TokenDialogMode = 'enroll' | 'rotate';

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
    const health = runner.health;
    // Prefer the per-class agent slot count when available (per-class gating).
    if (typeof health.available_agent_slots === 'number') return health.available_agent_slots;
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

function availableComputeSlots(runner: FleetRunner): number | null {
  if (runner.health) {
    const health = runner.health;
    if (typeof health.available_compute_slots === 'number') return health.available_compute_slots;
    if (typeof health.available_slots === 'number') return health.available_slots;
    if (health.available_slots === null) return null;
    if (typeof health.max_concurrent === 'number') {
      return Math.max(0, health.max_concurrent - activeWorkCount(runner));
    }
    return null;
  }
  const max = runner.capabilities.max_concurrent_compute ?? runner.capabilities.max_concurrent;
  if (typeof max !== 'number') return null;
  const activeCompute = Array.isArray(runner.capabilities.active_compute_job_ids)
    ? runner.capabilities.active_compute_job_ids.length
    : 0;
  return Math.max(0, max - activeCompute);
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

/** Desired-vs-applied config badge for a runner row. Pending = operator changed
 *  live-safe config but the daemon has not yet echoed the matching generation
 *  (applies on the next heartbeat); applied = the daemon's reported generation
 *  matches. Returns null for runners that never reported a config block. */
function configStatus(runner: FleetRunner): { label: string; cls: string; pending: boolean } | null {
  const config = runner.config;
  if (!config) return null;
  if (config.config_pending) {
    return {
      label: 'config pending — applying on next heartbeat',
      cls: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
      pending: true,
    };
  }
  // config_pending is false only once the daemon echoed the desired generation.
  if (config.applied_config_generation != null) {
    return {
      label: 'config applied',
      cls: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
      pending: false,
    };
  }
  return null;
}

/** Current GPU allowlist for a runner, from the server-authoritative desired
 *  config. Absent / "all" means no restriction (all visible GPUs eligible). */
function gpuAllowlistValue(runner: FleetRunner): 'all' | number[] {
  const allow = runner.config?.gpu_indices_allowed;
  if (Array.isArray(allow)) return allow;
  return 'all';
}

function gpuAllowlistLabel(runner: FleetRunner): string {
  const allow = gpuAllowlistValue(runner);
  if (allow === 'all') return 'GPUs: all';
  return allow.length > 0 ? `GPUs: ${allow.join(',')}` : 'GPUs: none';
}

/** Parse an operator-typed index list ("0, 1 2") into a deduped, sorted,
 *  non-negative integer array — the shape the PATCH endpoint normalizes to. */
function parseGpuIndexList(text: string): number[] {
  const seen = new Set<number>();
  const out: number[] = [];
  for (const token of text.split(/[\s,]+/)) {
    if (!token) continue;
    if (!/^\d+$/.test(token)) continue;
    const value = Number(token);
    if (!Number.isInteger(value) || value < 0 || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out.sort((a, b) => a - b);
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
    const agentReason = runner.health?.agent_unavailable_reason;
    const nextAction = agentReason ?? 'Restart or inspect the local agent';
    return { label: 'agent unavailable', detail, nextAction, tone: 'bad', ready: false };
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

/** Compact "ready for Workday / not ready (reason)" statement for a runner row.
 *  Workday (the resident strategic agent) runs on strategic-capable runners; for
 *  compute-only runners the identical readiness applies to general task dispatch. */
function workdayReadinessLine(
  readiness: RunnerReadiness,
  strategicRunner: boolean,
): { text: string; ready: boolean } {
  const target = strategicRunner ? 'Workday' : 'tasks';
  if (readiness.ready) return { text: `Ready for ${target}`, ready: true };
  return { text: `Not ready for ${target} — ${readiness.label}`, ready: false };
}

interface TokenDialogProps {
  runnerId: string;
  token: string;
  mode?: TokenDialogMode;
  preset?: RunnerSetupPreset;
  tokenExpiresAt?: string | null;
  /** True once the just-enrolled runner has heartbeated and shows connected. */
  connected: boolean;
  /** Hostname the connected runner reported, shown in the success line. */
  connectedHostname?: string | null;
  onDismiss: () => void;
}

function TokenDialog({
  runnerId,
  token,
  mode = 'enroll',
  preset = 'default',
  tokenExpiresAt,
  connected,
  connectedHostname,
  onDismiss,
}: TokenDialogProps) {
  const [copied, setCopied] = useState<Record<CopyTarget, boolean>>({
    token: false,
    windowsPowerShell: false,
    macosLinux: false,
  });
  // The runner POSTs heartbeats to the backend API, not the admin-UI hosting origin (Firebase
  // Hosting only serves the SPA and has no /api/** route). Prefer the configured API base URL;
  // fall back to window.location.origin only for same-origin dev where API_BASE_URL is empty.
  const origin =
    API_BASE_URL || (typeof window !== 'undefined' ? window.location.origin : 'https://your-orchestrator');
  const snippets = runnerSetupSnippets(token, origin);
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
          {mode === 'rotate'
            ? 'Runner token rotated'
            : preset === 'strategic_local'
              ? 'Strategic runner enrolled'
              : 'Runner enrolled'}
        </h3>
        <p className="text-xs text-amber-400 mb-3 font-medium">
          {mode === 'rotate'
            ? 'This replacement token is shown only once. Restart the existing runner process with it; the previous token no longer authenticates.'
            : 'This token is shown only once and cannot be retrieved again — copy it now.'}
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
          <div className="text-ui-md text-neutral-500">
            {mode === 'rotate'
              ? 'Stop the existing runner process, then restart it from the repo root with the new token.'
              : 'Run this from the repo root; the first heartbeat connects it below.'}
          </div>
        </div>

        {mode === 'rotate' ? (
          <div
            className="mb-4 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
            data-testid="runner-connection-status"
          >
            <KeyRound className="w-4 h-4 shrink-0 text-amber-300" />
            <span>Restart required. Existing heartbeats may still reflect the old process until it is restarted.</span>
          </div>
        ) : (
          <div
            className={`mb-4 flex items-center gap-2 rounded-md border px-3 py-2 text-xs ${
              connected
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                : 'border-amber-500/30 bg-amber-500/10 text-amber-300'
            }`}
            data-testid="runner-connection-status"
          >
            {connected ? (
              <>
                <Check className="w-4 h-4 shrink-0 text-emerald-400" />
                <span className="font-medium">
                  Connected on this project{connectedHostname ? ` — ${connectedHostname}` : ''}
                </span>
              </>
            ) : (
              <>
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-amber-400 animate-pulse" />
                <span>Waiting for your runner to connect…</span>
              </>
            )}
          </div>
        )}

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
  refreshSignal?: number;
  /** When true (WS stream connected), suppress the 30s blind poll — rely on
   *  refreshSignal and post-mutation loads instead. Absent or false keeps the
   *  poll running as a safety-net (WS down / prop not yet plumbed). */
  live?: boolean;
}

function tokenExpiryFromRegisterResult(result: FleetRunnerRegistrationResult): string | null {
  return result.token_expires_at ?? result.expires_at ?? result.expiresAt ?? null;
}

export function FleetRunnersPanel({ projectId, requireLocalRunner = false, refreshSignal, live }: Props) {
  const [runners, setRunners] = useState<FleetRunner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enrollingPreset, setEnrollingPreset] = useState<RunnerSetupPreset | null>(null);
  const [pendingToken, setPendingToken] = useState<{
    runnerId: string;
    token: string;
    mode: TokenDialogMode;
    preset: RunnerSetupPreset;
    tokenExpiresAt: string | null;
  } | null>(null);
  const [rotating, setRotating] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [editingCapabilities, setEditingCapabilities] = useState<string | null>(null);
  const [editMaxConcurrent, setEditMaxConcurrent] = useState<number>(2);
  const [editingGpuAllowlist, setEditingGpuAllowlist] = useState<string | null>(null);
  const [gpuAllowlistAll, setGpuAllowlistAll] = useState<boolean>(true);
  const [gpuAllowlistText, setGpuAllowlistText] = useState<string>('');
  const [updatingCapabilities, setUpdatingCapabilities] = useState<string | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<{ type: 'rotate' | 'revoke'; runner: FleetRunner } | null>(null);
  const [updatingStatus, setUpdatingStatus] = useState<string | null>(null);
  // Which runner's heartbeat time-series / stats detail is expanded (lazy-loaded).
  const [expandedTelemetry, setExpandedTelemetry] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // showLoading is opt-in so only the initial / projectId-change load surfaces
  // the full-panel spinner; background polls and refreshSignal reloads refresh
  // silently. Keeping setLoading(true) inside this callback (not in the effect
  // body) also keeps the effects free of a synchronous setState.
  const load = useCallback(async (showLoading = false) => {
    if (showLoading && mountedRef.current) setLoading(true);
    try {
      const data = await listFleetRunners(projectId);
      if (mountedRef.current) { setRunners(data); setError(null); }
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [projectId]);

  // load() is invoked through an effect-local async function so the setState
  // calls it performs land after an await (not synchronously in the effect
  // body) — the shape react-hooks/set-state-in-effect requires.
  useEffect(() => {
    async function run() { await load(true); }
    void run();
  }, [load]);
  useEffect(() => {
    if (refreshSignal === undefined) return;
    async function run() { await load(); }
    void run();
  }, [load, refreshSignal]);

  useEffect(() => {
    if (live === true) return;
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [load, live]);

  // While the enroll dialog is open, resolve the just-enrolled runner from the
  // list so the dialog can show live "waiting… / connected" feedback. Poll faster
  // than the 30s baseline until it connects, closing the enrollment loop the
  // operator was previously left guessing at.
  const enrolledRunner = pendingToken?.mode === 'enroll'
    ? runners.find(r => r.id === pendingToken.runnerId)
    : undefined;
  const enrolledConnected = enrolledRunner ? isRunnerLive(enrolledRunner) : false;

  useEffect(() => {
    if (!pendingToken || enrolledConnected) return;
    const interval = setInterval(load, 3000);
    return () => clearInterval(interval);
  }, [pendingToken, enrolledConnected, load]);

  const handleEnroll = async (preset: RunnerSetupPreset = 'default') => {
    setEnrollingPreset(preset);
    try {
      const result = preset === 'strategic_local'
        ? await registerFleetRunner(projectId, { preset: 'strategic_local' })
        : await registerFleetRunner(projectId);
      setPendingToken({
        runnerId: result.runner_id,
        token: result.token,
        mode: 'enroll',
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

  const handleRotate = (runner: FleetRunner) => {
    setPendingConfirm({ type: 'rotate', runner });
  };

  const handleRevoke = (runner: FleetRunner) => {
    setPendingConfirm({ type: 'revoke', runner });
  };

  const executeConfirmedAction = async () => {
    if (!pendingConfirm) return;
    const { type, runner } = pendingConfirm;
    setPendingConfirm(null);
    if (type === 'rotate') {
      setRotating(runner.id);
      try {
        const result = await rotateFleetRunnerToken(projectId, runner.id);
        setPendingToken({
          runnerId: result.runner_id,
          token: result.token,
          mode: 'rotate',
          preset: runner.capabilities.runner_preset === 'strategic_local' ? 'strategic_local' : 'default',
          tokenExpiresAt: tokenExpiryFromRegisterResult(result),
        });
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setRotating(null);
      }
    } else {
      setRevoking(runner.id);
      try {
        await revokeFleetRunner(projectId, runner.id);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setRevoking(null);
      }
    }
  };

  const handleUpdateCapabilities = async (runner: FleetRunner, maxConcurrent: number) => {
    setUpdatingCapabilities(runner.id);
    setEditingCapabilities(null);
    try {
      await updateRunnerCapabilities(projectId, runner.id, { max_concurrent: maxConcurrent });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUpdatingCapabilities(null);
    }
  };

  const handleUpdateGpuAllowlist = async (runner: FleetRunner, gpuIndicesAllowed: 'all' | number[]) => {
    setUpdatingCapabilities(runner.id);
    setEditingGpuAllowlist(null);
    try {
      await updateRunnerCapabilities(projectId, runner.id, { gpu_indices_allowed: gpuIndicesAllowed });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUpdatingCapabilities(null);
    }
  };

  const beginEditGpuAllowlist = (runner: FleetRunner) => {
    const current = gpuAllowlistValue(runner);
    setGpuAllowlistAll(current === 'all');
    setGpuAllowlistText(current === 'all' ? '' : current.join(','));
    setEditingGpuAllowlist(runner.id);
  };

  const handleUpdateStatus = async (runner: FleetRunner, status: 'active' | 'paused' | 'draining') => {
    setUpdatingStatus(runner.id);
    try {
      await patchRunnerStatus(projectId, runner.id, status);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUpdatingStatus(null);
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
            onClick={() => void load()}
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
            const isPaused = runner.status === 'paused';
            const isDraining = runner.status === 'draining';
            const runnerLive = isRunnerLive(runner);
            const activeCount = activeWorkCount(runner);
            const slots = runnerLive ? availableSlots(runner) : null;
            const computeSlots = runnerLive ? availableComputeSlots(runner) : null;
            const hasPerClassCapacity = runner.health?.max_concurrent_compute != null
              && runner.health.max_concurrent_compute !== runner.health.max_concurrent;
            const strategicRunner = isStrategicRunner(runner);
            const readiness = runnerReadiness(runner, runnerLive, slots);
            const workday = workdayReadinessLine(readiness, strategicRunner);
            const telemetry = telemetrySummary(runner);
            const configState = configStatus(runner);
            const backends = formatBackendInventory(runner.capabilities);
            const hasBackendInfo =
              backends.available.length > 0 || backends.agentBackend !== null || backends.computeBackend !== null;
            const telemetryOpen = expandedTelemetry === runner.id;
            return (
              <div
                key={runner.id}
                className="rounded-md border border-neutral-800 bg-[#0d1218]"
              >
              <div
                className="flex items-start gap-2.5 p-2.5"
                data-testid="fleet-runner-row"
              >
                <div className="mt-0.5 shrink-0">
                  <div className="flex h-7 w-7 items-center justify-center rounded-md border border-neutral-800 bg-[#151c25]">
                    <Cpu className="h-3.5 w-3.5 text-neutral-400" />
                  </div>
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className={`shrink-0 text-sm leading-none ${livCls}`} aria-hidden="true">●</span>
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
                    {isPaused && (
                      <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-xs font-medium text-amber-300">
                        paused
                      </span>
                    )}
                    {isDraining && (
                      <span className="rounded border border-blue-500/40 bg-blue-500/10 px-1.5 py-0.5 text-xs font-medium text-blue-300">
                        draining
                      </span>
                    )}
                    {!isActive && !isPaused && !isDraining && (
                      <span className="rounded border border-neutral-600/50 bg-neutral-700/40 px-1.5 py-0.5 text-xs text-neutral-300">
                        {runner.status === 'inactive' ? 'offline (deactivated)' : runner.status}
                      </span>
                    )}
                    {runnerLive && activeCount > 0 && (
                      <span className="rounded border border-blue-500/30 bg-blue-500/10 px-1.5 py-0.5 text-xs text-blue-300">
                        {activeCount} active
                      </span>
                    )}
                    {slots !== null && editingCapabilities === runner.id ? (
                      <span className="inline-flex items-center gap-1 rounded border border-blue-500/30 bg-blue-500/10 px-1.5 py-0.5 text-xs text-blue-300">
                        <input
                          type="number"
                          min={1}
                          max={64}
                          value={editMaxConcurrent}
                          onChange={e => setEditMaxConcurrent(Math.max(1, Math.min(64, Number(e.target.value) || 1)))}
                          className="w-10 bg-transparent text-center text-xs text-blue-300 outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                          autoFocus
                        />
                        <span>/ max</span>
                        {updatingCapabilities !== runner.id ? (
                          <>
                            <button
                              onClick={() => handleUpdateCapabilities(runner, editMaxConcurrent)}
                              className="text-emerald-400 hover:text-emerald-300 transition-colors min-h-[28px] min-w-[28px] flex items-center justify-center"
                              title="Save"
                              aria-label="Save max concurrent"
                            >
                              <Check className="w-3 h-3" />
                            </button>
                            <button
                              onClick={() => setEditingCapabilities(null)}
                              className="text-neutral-400 hover:text-neutral-200 transition-colors min-h-[28px] min-w-[28px] flex items-center justify-center"
                              title="Cancel"
                              aria-label="Cancel"
                            >
                              <span className="text-xs">✕</span>
                            </button>
                          </>
                        ) : (
                          <span className="text-blue-300 animate-pulse">saving…</span>
                        )}
                      </span>
                    ) : slots !== null && (
                      <span className="inline-flex items-center gap-1 rounded border border-neutral-700 bg-[#151c25] px-1.5 py-0.5 text-xs text-neutral-300">
                        {hasPerClassCapacity ? `${slots} agent slots free` : `${slots} slots free`}
                        {runner.capabilities.workload !== 'strategic' && runner.capabilities.runner_preset !== 'strategic_local' && (
                          <button
                            onClick={() => {
                              setEditMaxConcurrent(runner.capabilities.max_concurrent ?? 2);
                              setEditingCapabilities(runner.id);
                            }}
                            className="text-neutral-500 hover:text-neutral-200 transition-colors min-h-[20px] min-w-[20px] flex items-center justify-center"
                            title="Edit max concurrent"
                            aria-label="Edit max concurrent"
                          >
                            <Edit2 className="w-3 h-3" />
                          </button>
                        )}
                      </span>
                    )}
                    {hasPerClassCapacity && computeSlots !== null && (
                      <span className="inline-flex items-center gap-1 rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-xs text-purple-300">
                        {computeSlots} compute slots free
                      </span>
                    )}
                    {runner.capabilities.has_gpu && (
                      <span className="rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-xs text-purple-300">GPU</span>
                    )}
                    {runner.capabilities.has_gpu && (
                      editingGpuAllowlist === runner.id ? (
                        <span
                          className="inline-flex items-center gap-1 rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-xs text-purple-300"
                          data-testid="fleet-runner-gpu-allowlist-editor"
                        >
                          <label className="inline-flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={gpuAllowlistAll}
                              onChange={e => setGpuAllowlistAll(e.target.checked)}
                              aria-label="Allow all GPUs"
                            />
                            all
                          </label>
                          {!gpuAllowlistAll && (
                            <input
                              type="text"
                              inputMode="numeric"
                              value={gpuAllowlistText}
                              onChange={e => setGpuAllowlistText(e.target.value)}
                              placeholder="0,1"
                              aria-label="GPU indices allowed"
                              className="w-16 bg-transparent text-center text-xs text-purple-300 outline-none"
                              autoFocus
                            />
                          )}
                          {updatingCapabilities !== runner.id ? (
                            <>
                              <button
                                onClick={() => handleUpdateGpuAllowlist(runner, gpuAllowlistAll ? 'all' : parseGpuIndexList(gpuAllowlistText))}
                                className="text-emerald-400 hover:text-emerald-300 transition-colors min-h-[28px] min-w-[28px] flex items-center justify-center"
                                title="Save GPU allowlist"
                                aria-label="Save GPU allowlist"
                              >
                                <Check className="w-3 h-3" />
                              </button>
                              <button
                                onClick={() => setEditingGpuAllowlist(null)}
                                className="text-neutral-400 hover:text-neutral-200 transition-colors min-h-[28px] min-w-[28px] flex items-center justify-center"
                                title="Cancel"
                                aria-label="Cancel GPU allowlist"
                              >
                                <span className="text-xs">✕</span>
                              </button>
                            </>
                          ) : (
                            <span className="text-purple-300 animate-pulse">saving…</span>
                          )}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded border border-purple-500/30 bg-purple-500/10 px-1.5 py-0.5 text-xs text-purple-300">
                          {gpuAllowlistLabel(runner)}
                          <button
                            onClick={() => beginEditGpuAllowlist(runner)}
                            className="text-purple-300/70 hover:text-purple-200 transition-colors min-h-[20px] min-w-[20px] flex items-center justify-center"
                            title="Edit GPU allowlist"
                            aria-label="Edit GPU allowlist"
                          >
                            <Edit2 className="w-3 h-3" />
                          </button>
                        </span>
                      )
                    )}
                    {runner.health?.agent_available !== undefined && (
                      <span
                        className={`rounded border px-1.5 py-0.5 text-xs ${
                          runner.health.agent_available
                            ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                            : 'border-red-500/30 bg-red-500/10 text-red-300'
                        }`}
                        title={!runner.health.agent_available && runner.health.agent_unavailable_reason ? runner.health.agent_unavailable_reason : undefined}
                      >
                        {runner.health.agent_available
                          ? 'agent ✓'
                          : runner.health.agent_unavailable_reason
                            ? `agent ✗: ${runner.health.agent_unavailable_reason}`
                            : 'agent ✗'}
                      </span>
                    )}
                    <span className={`rounded border px-1.5 py-0.5 text-xs font-medium ${readinessClasses(readiness.tone)}`}>
                      {readiness.label}
                    </span>
                    {configState && (
                      <span
                        className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-medium ${configState.cls}`}
                        data-testid="fleet-runner-config-status"
                      >
                        {configState.pending
                          ? <RefreshCw className="w-3 h-3 animate-spin" />
                          : <Check className="w-3 h-3" />}
                        {configState.label}
                      </span>
                    )}
                  </div>
                  <div
                    className={`mt-1 text-xs font-medium ${workday.ready ? 'text-emerald-300' : 'text-amber-300'}`}
                    data-testid="fleet-runner-workday-readiness"
                  >
                    {workday.ready ? '✓ ' : '⚠ '}{workday.text}
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
                  {hasBackendInfo && (
                    <div className="mt-0.5 flex flex-wrap items-center gap-1" data-testid="fleet-runner-backends">
                      <span className="text-ui-md text-neutral-600">Backends:</span>
                      {backends.available.map(name => {
                        const selected = name === backends.agentBackend || name === backends.computeBackend;
                        return (
                          <span
                            key={name}
                            className={`rounded border px-1.5 py-0.5 text-xs ${
                              selected
                                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                                : 'border-neutral-700 bg-[#151c25] text-neutral-300'
                            }`}
                          >
                            {name}
                          </span>
                        );
                      })}
                      {backends.agentBackend && (
                        <span className="text-ui-md text-emerald-400/80">agent→{backends.agentBackend}</span>
                      )}
                      {backends.computeBackend && (
                        <span className="text-ui-md text-emerald-400/80">compute→{backends.computeBackend}</span>
                      )}
                    </div>
                  )}
                  <div className="mt-0.5 text-ui-md text-neutral-600">{readiness.detail}</div>
                  <div className={`mt-0.5 text-ui-md ${readiness.ready ? 'text-emerald-400/80' : 'text-amber-300/90'}`}>
                    Next: {readiness.nextAction}
                  </div>
                </div>
                {(isActive || isDraining) && (
                  <button
                    onClick={() => void handleUpdateStatus(runner, 'paused')}
                    disabled={updatingStatus === runner.id}
                    className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-amber-500/10 hover:text-amber-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
                    aria-label={`Pause runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                    title="Pause runner (stop new dispatch)"
                  >
                    <PauseCircle className="w-3.5 h-3.5" />
                  </button>
                )}
                {isActive && (
                  <button
                    onClick={() => void handleUpdateStatus(runner, 'draining')}
                    disabled={updatingStatus === runner.id}
                    className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-blue-500/10 hover:text-blue-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
                    aria-label={`Drain runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                    title="Drain runner (finish existing, skip new)"
                  >
                    <Wind className="w-3.5 h-3.5" />
                  </button>
                )}
                {(isPaused || isDraining) && (
                  <button
                    onClick={() => void handleUpdateStatus(runner, 'active')}
                    disabled={updatingStatus === runner.id}
                    className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-emerald-500/10 hover:text-emerald-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
                    aria-label={`Resume runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                    title="Resume runner (re-enable dispatch)"
                  >
                    <PlayCircle className="w-3.5 h-3.5" />
                  </button>
                )}
                <button
                  onClick={() => setExpandedTelemetry(telemetryOpen ? null : runner.id)}
                  className={`flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md transition-colors hover:bg-sky-500/10 hover:text-sky-300 md:min-h-9 md:min-w-9 ${telemetryOpen ? 'text-sky-300' : 'text-neutral-500'}`}
                  aria-label={`Toggle telemetry for runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                  aria-expanded={telemetryOpen}
                  title="Heartbeat telemetry & stats (last hour)"
                >
                  <Activity className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => handleRotate(runner)}
                  disabled={rotating === runner.id}
                  className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-amber-500/10 hover:text-amber-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
                  aria-label={`Rotate token for runner ${runner.hostname ?? runner.id.slice(0, 8)}`}
                  title="Rotate runner token"
                >
                  <KeyRound className="w-3.5 h-3.5" />
                </button>
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
              {telemetryOpen && (
                <RunnerTelemetryDetail projectId={projectId} runnerId={runner.id} />
              )}
              </div>
            );
          })}
        </div>
      )}

      {pendingToken && (
        <TokenDialog
          runnerId={pendingToken.runnerId}
          token={pendingToken.token}
          mode={pendingToken.mode}
          preset={pendingToken.preset}
          tokenExpiresAt={pendingToken.tokenExpiresAt}
          connected={pendingToken.mode === 'enroll' ? enrolledConnected : false}
          connectedHostname={pendingToken.mode === 'enroll' ? enrolledRunner?.hostname : null}
          onDismiss={() => setPendingToken(null)}
        />
      )}

      {pendingConfirm && (
        <ConfirmDialog
          title={pendingConfirm.type === 'rotate' ? 'Rotate runner token' : 'Revoke runner'}
          message={pendingConfirm.type === 'rotate'
            ? `Rotate token for runner ${pendingConfirm.runner.hostname ?? pendingConfirm.runner.id}? The current token will stop working immediately.`
            : `Revoke runner ${pendingConfirm.runner.hostname ?? pendingConfirm.runner.id}? It will no longer be able to authenticate.`}
          confirmLabel={pendingConfirm.type === 'rotate' ? 'Rotate token' : 'Revoke'}
          onConfirm={() => void executeConfirmedAction()}
          onCancel={() => setPendingConfirm(null)}
        />
      )}
    </div>
  );
}

/** Minimal dependency-free sparkline: scales a (possibly sparse) numeric series
 *  into an inline SVG polyline. Nulls break the line rather than plotting zero. */
function Sparkline({
  values,
  width = 140,
  height = 28,
  color = '#38bdf8',
  unit = '',
}: {
  values: (number | null)[];
  width?: number;
  height?: number;
  color?: string;
  unit?: string;
}) {
  const present = values.filter((v): v is number => v != null);
  if (present.length === 0) {
    return <span className="text-ui-md text-neutral-600">no data</span>;
  }
  const min = Math.min(...present);
  const max = Math.max(...present);
  const span = max - min || 1;
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  let d = '';
  let started = false;
  values.forEach((v, i) => {
    if (v == null) {
      started = false;
      return;
    }
    const x = i * stepX;
    const y = height - ((v - min) / span) * height;
    d += `${started ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
    started = true;
  });
  const last = present[present.length - 1];
  return (
    <span className="inline-flex items-center gap-1.5">
      <svg width={width} height={height} aria-hidden="true">
        <path d={d.trim()} fill="none" stroke={color} strokeWidth={1.5} />
      </svg>
      <span className="text-ui-md tabular-nums text-neutral-400">
        {last.toLocaleString(undefined, { maximumFractionDigits: 1 })}
        {unit}
      </span>
    </span>
  );
}

/** Expandable per-runner detail: heartbeat time-series sparklines (GPU/disk/mem
 *  over the last hour) + compute-job rollups. Lazy-loaded when the row expands. */
function RunnerTelemetryDetail({ projectId, runnerId }: { projectId: string; runnerId: string }) {
  const [telemetry, setTelemetry] = useState<RunnerTelemetryResponse | null>(null);
  const [stats, setStats] = useState<RunnerStatsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      fetchRunnerTelemetry(projectId, runnerId, { sinceSeconds: 3600 }),
      fetchRunnerStats(projectId, runnerId),
    ])
      .then(([t, s]) => {
        if (cancelled) return;
        setTelemetry(t);
        setStats(s);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Failed to load telemetry');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, runnerId]);

  if (loading) {
    return (
      <div className="border-t border-neutral-800 px-2.5 py-2 text-ui-md text-neutral-500">
        Loading telemetry…
      </div>
    );
  }
  if (error) {
    return (
      <div className="border-t border-neutral-800 px-2.5 py-2 text-ui-md text-amber-300/90">
        {error}
      </div>
    );
  }

  const snaps = telemetry?.snapshots ?? [];
  const gpu = snaps.map(s => s.gpu_utilization_pct);
  const diskGb = snaps.map(s => (s.disk_free_bytes != null ? s.disk_free_bytes / 1e9 : null));
  const memGb = snaps.map(s => (s.memory_free_bytes != null ? s.memory_free_bytes / 1e9 : null));
  const successPct = stats?.jobs_success_rate != null ? Math.round(stats.jobs_success_rate * 100) : null;

  return (
    <div
      className="space-y-2 border-t border-neutral-800 px-2.5 py-2"
      data-testid="fleet-runner-telemetry-detail"
    >
      {snaps.length === 0 ? (
        <div className="text-ui-md text-neutral-600">No heartbeat snapshots in the last hour.</div>
      ) : (
        <div className="grid grid-cols-[5rem_1fr] items-center gap-x-2 gap-y-1">
          <span className="text-ui-md text-neutral-500">GPU util</span>
          <Sparkline values={gpu} color="#38bdf8" unit="%" />
          <span className="text-ui-md text-neutral-500">Disk free</span>
          <Sparkline values={diskGb} color="#34d399" unit=" GB" />
          <span className="text-ui-md text-neutral-500">Mem free</span>
          <Sparkline values={memGb} color="#a78bfa" unit=" GB" />
        </div>
      )}
      <div className="text-ui-md text-neutral-600">
        {telemetry?.count ?? 0} snapshots · last hour
      </div>
      {stats && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui-md">
          <span className="text-neutral-400">
            Jobs run: <span className="tabular-nums text-neutral-200">{stats.jobs_run_count}</span>
          </span>
          <span className="text-neutral-400">
            Success:{' '}
            <span className="tabular-nums text-neutral-200">
              {successPct != null ? `${successPct}%` : '—'}
            </span>
            {stats.jobs_terminal_count > 0 && (
              <span className="text-neutral-600">
                {' '}
                ({stats.jobs_succeeded_count}/{stats.jobs_terminal_count})
              </span>
            )}
          </span>
          <span className="text-neutral-400" title="On-demand cloud VM cost these self-hosted runs avoided">
            Cloud cost avoided:{' '}
            <span className="tabular-nums text-emerald-300">
              ${(stats.cloud_cost_avoided_usd ?? 0).toFixed(2)}
            </span>
          </span>
        </div>
      )}
      {stats && stats.claim_latency_sample_count > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui-md">
          <span className="text-neutral-400" title="Dispatch-to-claim pickup latency across completed tasks">
            Claim latency:{' '}
            <span className="tabular-nums text-neutral-200">
              {stats.claim_latency_ms_p50 != null ? `${stats.claim_latency_ms_p50}ms` : '—'}
            </span>{' '}
            <span className="text-neutral-600">p50</span>
          </span>
          <span className="text-neutral-400">
            <span className="tabular-nums text-neutral-200">
              {stats.claim_latency_ms_p95 != null ? `${stats.claim_latency_ms_p95}ms` : '—'}
            </span>{' '}
            <span className="text-neutral-600">p95</span>
          </span>
          <span className="text-neutral-400">
            <span className="tabular-nums text-neutral-200">
              {stats.claim_latency_ms_p99 != null ? `${stats.claim_latency_ms_p99}ms` : '—'}
            </span>{' '}
            <span className="text-neutral-600">p99</span>
          </span>
          <span className="text-neutral-600">
            ({stats.claim_latency_sample_count} samples)
          </span>
        </div>
      )}
      {stats && stats.last_n_outcomes.length > 0 && (
        <div className="space-y-0.5">
          <span className="text-ui-md text-neutral-600">Recent jobs:</span>
          {stats.last_n_outcomes.map(o => (
            <div key={o.job_id} className="flex items-center gap-2 text-ui-md">
              <span
                className={`tabular-nums ${
                  o.status === 'SUCCEEDED'
                    ? 'text-emerald-400/80'
                    : o.status === 'FAILED' || o.status === 'TIMED_OUT'
                      ? 'text-red-400/80'
                      : 'text-neutral-400'
                }`}
              >
                {o.status}
              </span>
              {(o.status === 'FAILED' || o.status === 'TIMED_OUT') &&
                (o.terminal_reason || o.exit_code != null) && (
                  <span
                    className="truncate text-red-400/70"
                    title={o.terminal_reason ?? undefined}
                  >
                    {o.terminal_reason ?? ''}
                    {o.exit_code != null ? ` (exit ${o.exit_code})` : ''}
                  </span>
                )}
              {o.task_id && (
                <span className="truncate text-neutral-600">task {o.task_id.slice(0, 8)}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
