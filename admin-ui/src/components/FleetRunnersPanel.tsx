import { useState, useCallback, useEffect, useRef } from 'react';
import { Cpu, Copy, Check, RefreshCw, Trash2 } from 'lucide-react';
import { listFleetRunners, registerFleetRunner, revokeFleetRunner } from '../api';
import type { FleetRunner } from '../api';
import { timeAgo, parseTimestamp } from '../utils/time';

// A runner is "live" (eligible for dispatch) only if it heartbeated within this
// window. Mirrors the backend's `_RUNNER_LIVENESS_SECONDS` (orchestrator.py): a
// runner past this cutoff is swept to status='inactive' and no longer dispatched
// to, so the UI must not show "live" past it. Beyond the stale window it reads
// fully offline.
const LIVENESS_THRESHOLD_SEC = 300;
const STALE_THRESHOLD_SEC = 600;

function livenessLabel(lastSeenAt: string | null): { label: string; cls: string } {
  if (!lastSeenAt) return { label: 'never seen', cls: 'text-neutral-500' };
  const ageMs = Date.now() - parseTimestamp(lastSeenAt).getTime();
  const ageSec = ageMs / 1000;
  if (ageSec < LIVENESS_THRESHOLD_SEC) return { label: 'live', cls: 'text-emerald-400' };
  if (ageSec < STALE_THRESHOLD_SEC) return { label: 'stale', cls: 'text-amber-400' };
  return { label: 'offline', cls: 'text-neutral-500' };
}

/** Count of tasks the runner reported executing in its last heartbeat. */
function activeTaskCount(caps: FleetRunner['capabilities']): number {
  const ids = caps.active_task_ids;
  return Array.isArray(ids) ? ids.length : 0;
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

interface TokenDialogProps {
  runnerId: string;
  token: string;
  onDismiss: () => void;
}

function TokenDialog({ runnerId, token, onDismiss }: TokenDialogProps) {
  const [copied, setCopied] = useState(false);
  const origin = typeof window !== 'undefined' ? window.location.origin : 'https://your-orchestrator';
  const snippet = `BLUEPRINT_RUNNER_TOKEN=${token} BLUEPRINT_ORCHESTRATOR_URL=${origin} python3 scripts/local_runner.py`;

  const copy = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" />
      <div className="relative z-10 w-full max-w-lg bg-neutral-900 border border-neutral-700 rounded-lg p-5 shadow-2xl mx-4">
        <h3 className="text-sm font-semibold text-neutral-200 mb-1">Runner enrolled</h3>
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
}

export function FleetRunnersPanel({ projectId }: Props) {
  const [runners, setRunners] = useState<FleetRunner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enrolling, setEnrolling] = useState(false);
  const [pendingToken, setPendingToken] = useState<{ runnerId: string; token: string } | null>(null);
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

  const handleEnroll = async () => {
    setEnrolling(true);
    try {
      const result = await registerFleetRunner(projectId);
      setPendingToken({ runnerId: result.runner_id, token: result.token });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setEnrolling(false);
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

  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-semibold text-neutral-400 uppercase tracking-wider">Self-hosted runners</h3>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            className="text-neutral-400 hover:text-neutral-200 transition-colors p-1 min-w-[44px] min-h-[44px] flex items-center justify-center"
            title="Refresh"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={handleEnroll}
            disabled={enrolling}
            className="px-3 py-1.5 text-xs font-medium bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors min-h-[44px]"
          >
            {enrolling ? 'Enrolling…' : 'Enroll runner'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 text-xs text-red-400 bg-red-400/10 border border-red-400/20 rounded px-3 py-2">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-xs text-neutral-500 py-4 text-center">Loading…</div>
      ) : runners.length === 0 ? (
        <div className="text-xs text-neutral-500 py-6 text-center border border-dashed border-neutral-800 rounded">
          No runners enrolled. Click <span className="text-neutral-300">Enroll runner</span> to register one.
        </div>
      ) : (
        <div className="space-y-2">
          {runners.map(runner => {
            const { label: livLabel, cls: livCls } = livenessLabel(runner.last_seen_at);
            // status is authoritative (set by the backend stale-runner sweep);
            // distinct from the timestamp-derived live/stale label above.
            const isActive = runner.status === 'active';
            const activeCount = activeTaskCount(runner.capabilities);
            return (
              <div
                key={runner.id}
                className="bg-neutral-900 border border-neutral-800 rounded p-3 flex items-start gap-3"
              >
                <div className="shrink-0 mt-0.5">
                  <div className="w-7 h-7 rounded bg-neutral-800 flex items-center justify-center">
                    <Cpu className="w-3.5 h-3.5 text-neutral-400" />
                  </div>
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-neutral-200 truncate">
                      {runner.hostname ?? runner.id.slice(0, 8)}
                    </span>
                    {runner.version && (
                      <span className="text-xs text-neutral-500">{runner.version}</span>
                    )}
                    <span className={`text-xs font-medium ${livCls}`}>{livLabel}</span>
                    {!isActive && (
                      <span className="text-xs bg-neutral-700/40 border border-neutral-600/50 text-neutral-300 rounded px-1.5 py-0.5">
                        {runner.status === 'inactive' ? 'offline (deactivated)' : runner.status}
                      </span>
                    )}
                    {activeCount > 0 && (
                      <span className="text-xs bg-blue-500/10 border border-blue-500/30 text-blue-400 rounded px-1.5 py-0.5">
                        {activeCount} active
                      </span>
                    )}
                    {runner.capabilities.has_gpu && (
                      <span className="text-xs bg-purple-500/10 border border-purple-500/30 text-purple-400 rounded px-1.5 py-0.5">GPU</span>
                    )}
                  </div>
                  <div className="text-xs text-neutral-500 mt-0.5">{capsSummary(runner.capabilities)}</div>
                  {runner.last_seen_at && (
                    <div className="text-xs text-neutral-600 mt-0.5">
                      last seen {timeAgo(runner.last_seen_at)}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => handleRevoke(runner)}
                  disabled={revoking === runner.id}
                  className="shrink-0 text-neutral-500 hover:text-red-400 transition-colors p-1 min-w-[44px] min-h-[44px] flex items-center justify-center disabled:opacity-40"
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
          onDismiss={() => setPendingToken(null)}
        />
      )}
    </div>
  );
}
