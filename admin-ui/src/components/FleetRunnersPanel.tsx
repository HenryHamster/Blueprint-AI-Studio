import { useState, useCallback, useEffect, useRef } from 'react';
import { Compass, Cpu, Copy, Check, RefreshCw, Trash2 } from 'lucide-react';
import { listFleetRunners, registerFleetRunner, revokeFleetRunner } from '../api';
import type { FleetRunner } from '../api';
import { timeAgo, parseTimestamp } from '../utils/time';

const LIVENESS_THRESHOLD_SEC = 300;
const STALE_THRESHOLD_SEC = 600;

function fallbackLivenessLabel(lastSeenAt: string | null): { label: string; cls: string } {
  if (!lastSeenAt) return { label: 'never seen', cls: 'text-neutral-500' };
  const ageMs = Date.now() - parseTimestamp(lastSeenAt).getTime();
  const ageSec = ageMs / 1000;
  if (ageSec < LIVENESS_THRESHOLD_SEC) return { label: 'live', cls: 'text-emerald-400' };
  if (ageSec < STALE_THRESHOLD_SEC) return { label: 'stale', cls: 'text-amber-400' };
  return { label: 'offline', cls: 'text-neutral-500' };
}

function livenessLabel(runner: FleetRunner): { label: string; cls: string } {
  if (runner.status !== 'active') return { label: 'offline', cls: 'text-neutral-500' };
  const health = runner.health;
  if (!health) return fallbackLivenessLabel(runner.last_seen_at);
  if (health.is_live) return { label: 'live', cls: 'text-emerald-400' };
  if (health.stale_reason === 'heartbeat_stale') return { label: 'stale', cls: 'text-amber-400' };
  if (health.stale_reason === 'never_seen') return { label: 'never seen', cls: 'text-neutral-500' };
  return { label: 'offline', cls: 'text-neutral-500' };
}

function isRunnerLive(runner: FleetRunner): boolean {
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
  const activeCompute = Array.isArray(runner.capabilities.active_compute_job_ids)
    ? runner.capabilities.active_compute_job_ids.length
    : 0;
  return activeTaskCount(runner) + activeCompute;
}

function availableSlots(runner: FleetRunner): number | null {
  if (runner.health) return runner.health.available_slots;
  const max = runner.capabilities.max_concurrent;
  if (typeof max !== 'number') return null;
  return Math.max(0, max - activeTaskCount(runner));
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

interface TokenDialogProps {
  runnerId: string;
  token: string;
  preset?: 'default' | 'strategic_local';
  onDismiss: () => void;
}

function TokenDialog({ runnerId, token, preset = 'default', onDismiss }: TokenDialogProps) {
  const [copied, setCopied] = useState(false);
  const origin = typeof window !== 'undefined' ? window.location.origin : 'https://your-orchestrator';
  const modePrefix = preset === 'strategic_local' ? 'BLUEPRINT_RUNNER_MAX_CONCURRENT=1 ' : '';
  const snippet = `${modePrefix}BLUEPRINT_RUNNER_TOKEN=${token} BLUEPRINT_ORCHESTRATOR_URL=${origin} python3 scripts/local_runner.py`;

  const copy = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" />
      <div className="relative z-10 w-full max-w-lg bg-neutral-900 border border-neutral-700 rounded-lg p-5 shadow-2xl mx-4">
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
              onClick={() => copy(token)}
              className="shrink-0 text-neutral-400 hover:text-neutral-200 transition-colors p-1 min-w-[44px] min-h-[44px] flex items-center justify-center"
              title="Copy token"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        <div className="mb-4">
          <div className="text-xs text-neutral-400 mb-1">Setup snippet</div>
          <div className="flex items-start gap-2 bg-neutral-950 border border-neutral-700 rounded px-3 py-2">
            <code className="flex-1 text-xs text-neutral-300 break-all font-mono whitespace-pre-wrap">{snippet}</code>
            <button
              onClick={() => copy(snippet)}
              className="shrink-0 text-neutral-400 hover:text-neutral-200 transition-colors p-1 min-w-[44px] min-h-[44px] flex items-center justify-center"
              title="Copy setup snippet"
            >
              <Copy className="w-3.5 h-3.5" />
            </button>
          </div>
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

export function FleetRunnersPanel({ projectId, requireLocalRunner = false }: Props) {
  const [runners, setRunners] = useState<FleetRunner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enrollingPreset, setEnrollingPreset] = useState<'default' | 'strategic_local' | null>(null);
  const [pendingToken, setPendingToken] = useState<{ runnerId: string; token: string; preset: 'default' | 'strategic_local' } | null>(null);
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

  const handleEnroll = async (preset: 'default' | 'strategic_local' = 'default') => {
    setEnrollingPreset(preset);
    try {
      const result = preset === 'strategic_local'
        ? await registerFleetRunner(projectId, { preset: 'strategic_local' })
        : await registerFleetRunner(projectId);
      setPendingToken({ runnerId: result.runner_id, token: result.token, preset });
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

  const liveRunners = runners.filter(isRunnerLive);
  const liveComputeRunners = liveRunners.filter(isComputeCapable);
  const liveGpuRunners = liveRunners.filter(runner => runner.capabilities.has_gpu);
  const liveStrategicRunners = liveRunners.filter(isStrategicRunner);
  const busyWork = liveRunners.reduce((sum, runner) => sum + activeWorkCount(runner), 0);
  const maxSlots = liveRunners.reduce((sum, runner) => {
    const max = runner.health?.max_concurrent ?? runner.capabilities.max_concurrent;
    return sum + (typeof max === 'number' ? max : 0);
  }, 0);

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
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">live</div>
            <div className="mt-0.5 font-mono text-neutral-100">{liveRunners.length}/{runners.length}</div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">work</div>
            <div className="mt-0.5 font-mono text-neutral-100" data-testid="fleet-runner-work-capacity">{busyWork}/{maxSlots || '-'}</div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">gpu</div>
            <div className={`mt-0.5 font-mono ${liveGpuRunners.length > 0 ? 'text-purple-300' : 'text-neutral-500'}`}>
              {liveGpuRunners.length > 0 ? `${liveGpuRunners.length} online` : 'offline'}
            </div>
          </div>
          <div className="rounded-md border border-neutral-800 bg-[#0d1218] px-2.5 py-2">
            <div className="text-ui-md uppercase tracking-wide text-neutral-500">strategic</div>
            <div className={`mt-0.5 font-mono ${liveStrategicRunners.length > 0 ? 'text-emerald-300' : 'text-neutral-500'}`} data-testid="fleet-runner-strategic-readiness">
              {liveStrategicRunners.length > 0 ? `${liveStrategicRunners.length} live` : 'none live'}
            </div>
          </div>
        </div>
      )}

      {requireLocalRunner && !loading && liveComputeRunners.length === 0 && (
        <div className="rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          Local-only compute is enabled for this project. Compute experiments will wait until a live compute-capable runner connects.
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
            const activeCount = activeTaskCount(runner);
            const slots = runnerLive ? availableSlots(runner) : null;
            const strategicRunner = isStrategicRunner(runner);
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
                  </div>
                  <div className="mt-1 text-xs text-neutral-500">{capsSummary(runner.capabilities)}</div>
                  {runner.last_seen_at && (
                    <div className="mt-0.5 text-ui-md text-neutral-600">
                      last seen {timeAgo(runner.last_seen_at)}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => handleRevoke(runner)}
                  disabled={revoking === runner.id}
                  className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-red-500/10 hover:text-red-300 disabled:opacity-40 md:min-h-9 md:min-w-9"
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
          onDismiss={() => setPendingToken(null)}
        />
      )}
    </div>
  );
}
