import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle, ChevronDown, ChevronRight, CircleDollarSign, GitBranch, RefreshCw, TimerReset, XCircle } from 'lucide-react';
import { drainLanePilot, fetchImprovementLanes, fetchLanePilotPreflight, type ImprovementLaneRow, type LanePilotDrainReport, type LanePilotPreflightResponse, type ShadowLaneHealth } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute, formatDurationSec, parseTimestamp } from '../utils/time';
import { ConfirmDialog } from './ConfirmDialog';

const STORAGE_KEY = 'blueprint-admin:improvementLanesPanelCollapsed';

interface ImprovementLanesPanelProps {
  projectId: string | null;
}

function readCollapsedPreference(): boolean | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === null ? null : stored === 'true';
  } catch {
    return null;
  }
}

function writeCollapsedPreference(value: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(value));
  } catch {
    /* localStorage unavailable — keep in-memory state */
  }
}

function formatAgeSeconds(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return '—';
  return `${formatDurationSec(seconds)} ago`;
}

function formatUsd(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `$${value.toFixed(2)}`;
}

function statusClasses(status: string): string {
  switch (status.toUpperCase()) {
    case 'ACTIVE':
      return 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300';
    case 'PARKED':
      return 'border-amber-500/30 bg-amber-500/15 text-amber-300';
    case 'PROPOSED':
      return 'border-slate-500/30 bg-slate-500/15 text-slate-300';
    case 'RETIRED':
      return 'border-sky-500/30 bg-sky-500/15 text-sky-300';
    case 'KILLED':
      return 'border-red-500/30 bg-red-500/15 text-red-300';
    default:
      return 'border-neutral-700 bg-neutral-800 text-neutral-300';
  }
}

function shadowClasses(shadow: boolean): string {
  return shadow
    ? 'border-orange-500/30 bg-orange-500/15 text-orange-300'
    : 'border-neutral-700 bg-neutral-800 text-neutral-300';
}

export function ImprovementLanesPanel({ projectId }: ImprovementLanesPanelProps) {
  const [lanes, setLanes] = useState<ImprovementLaneRow[]>([]);
  const [shadowHealth, setShadowHealth] = useState<ShadowLaneHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsedPref, setCollapsedPref] = useState<boolean | null>(() => readCollapsedPreference());
  const [showDrainConfirm, setShowDrainConfirm] = useState(false);
  const [draining, setDraining] = useState(false);
  const [drainResult, setDrainResult] = useState<LanePilotDrainReport | null>(null);
  const [drainError, setDrainError] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<LanePilotPreflightResponse | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setLoading(true);
    setFetchError(null);
    try {
      const [lanesData, preflightData] = await Promise.all([
        fetchImprovementLanes(projectId, 50, 0, signal),
        fetchLanePilotPreflight(projectId, signal).catch(() => null),
      ]);
      if (signal?.aborted) return;
      setLanes(lanesData.lanes ?? []);
      setShadowHealth(lanesData.shadow_health ?? null);
      setPreflight(preflightData);
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      setLanes([]);
      setFetchError(e instanceof Error ? e.message : 'Failed to load improvement lanes');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [projectId]);

  const handleDrainConfirm = useCallback(async () => {
    if (!projectId) return;
    setShowDrainConfirm(false);
    setDraining(true);
    setDrainError(null);
    setDrainResult(null);
    try {
      const report = await drainLanePilot(projectId);
      setDrainResult(report);
      await load();
    } catch (e) {
      setDrainError(e instanceof Error ? e.message : 'Failed to drain lane pilot');
    } finally {
      setDraining(false);
    }
  }, [projectId, load]);

  useEffect(() => {
    setLanes([]);
    setShadowHealth(null);
    setFetchError(null);
    setPreflight(null);
    setLoading(Boolean(projectId));
  }, [projectId]);

  usePoller((signal) => load(signal), {
    intervalMs: DEFAULT_POLL_MS,
    enabled: !!projectId,
    deps: [projectId],
  });

  const toggleCollapsed = useCallback(() => {
    setCollapsedPref((prev) => {
      const next = !(prev ?? (lanes.length === 0 && !loading));
      writeCollapsedPreference(next);
      return next;
    });
  }, [lanes.length, loading]);

  if (!projectId) return null;

  const hasLanes = lanes.length > 0;
  const collapsed = collapsedPref ?? (!loading && !hasLanes && !fetchError);
  const activeCount = lanes.filter((lane) => lane.status?.toUpperCase() === 'ACTIVE').length;
  const shadowCount = lanes.filter((lane) => lane.shadow).length;

  return (
    <div className="mb-3 shrink-0" data-testid="improvement-lanes-panel">
      <button
        type="button"
        onClick={toggleCollapsed}
        data-testid="improvement-lanes-toggle"
        aria-expanded={!collapsed}
        className="flex flex-wrap min-h-[44px] w-full items-center gap-2 px-1 py-1 text-[11px] font-bold uppercase tracking-widest text-neutral-400 transition-colors hover:text-neutral-300"
      >
        {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        <GitBranch className="h-3 w-3" />
        Improvement lanes
        {hasLanes && (
          <span className="ml-1 rounded-full border border-neutral-700 bg-neutral-800 px-1.5 py-0.5 text-[10px] font-bold leading-none text-neutral-300">
            {lanes.length} lanes
          </span>
        )}
        {activeCount > 0 && (
          <span className="rounded-full border border-emerald-500/30 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-emerald-300">
            {activeCount} active
          </span>
        )}
        {shadowCount > 0 && (
          <span
            data-testid="improvement-lanes-shadow-summary"
            className="rounded-full border border-orange-500/30 bg-orange-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-orange-300"
          >
            {shadowCount} shadow
          </span>
        )}
        {shadowHealth && shadowHealth.shadow_lane_count > 0 && shadowHealth.miss_rate != null && (
          <span
            data-testid="improvement-lanes-shadow-miss-rate"
            className={`rounded-full border px-1.5 py-0.5 text-[10px] font-bold leading-none ${shadowHealth.miss_rate > 0 ? 'border-amber-500/30 bg-amber-500/15 text-amber-300' : 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300'}`}
          >
            miss {(shadowHealth.miss_rate * 100).toFixed(0)}%
          </span>
        )}
        {collapsed && fetchError && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-red-300">
            Error
          </span>
        )}
      </button>

      {showDrainConfirm && (
        <ConfirmDialog
          title="Drain / Kill Lane Pilot"
          message="This stops all future lane assignment for this project, releases warm-pool reservations held by lanes, and kills all in-flight lanes (terminal KILLED state). This is the durable operator equivalent of an automated pilot KILL — not a reversible flag toggle."
          confirmLabel="Drain & Kill"
          onConfirm={handleDrainConfirm}
          onCancel={() => setShowDrainConfirm(false)}
        />
      )}

      {!collapsed && (
        <div className="mt-1 rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-xs">
          {preflight && (
            <div
              data-testid="lane-pilot-preflight"
              className={`mb-3 rounded border p-2 ${preflight.ready ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-amber-500/30 bg-amber-500/10'}`}
            >
              <div className="mb-1.5 flex items-center gap-1.5">
                {preflight.ready
                  ? <CheckCircle className="h-3 w-3 shrink-0 text-emerald-400" />
                  : <AlertTriangle className="h-3 w-3 shrink-0 text-amber-400" />}
                <span className={`font-semibold ${preflight.ready ? 'text-emerald-300' : 'text-amber-300'}`}>
                  {preflight.ready ? 'Pilot ready to enable' : 'Prerequisites unmet'}
                </span>
                {preflight.pilot_mode && preflight.pilot_mode !== 'off' && (
                  <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${preflight.pilot_mode === 'on' ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : 'border-amber-500/40 bg-amber-500/15 text-amber-300'}`}>
                    {preflight.pilot_mode === 'on' ? 'pilot on' : 'shadow'}
                  </span>
                )}
                {preflight.durable_verdict && (
                  <span className={`ml-auto rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${preflight.durable_verdict === 'KILL' ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'}`}>
                    {preflight.durable_verdict}
                  </span>
                )}
              </div>
              <div className="space-y-0.5">
                {preflight.checks.map((check) => (
                  <div key={check.name} className="flex items-start gap-1.5">
                    {check.met
                      ? <CheckCircle className="mt-0.5 h-2.5 w-2.5 shrink-0 text-emerald-500" />
                      : <XCircle className="mt-0.5 h-2.5 w-2.5 shrink-0 text-red-400" />}
                    <span className={`min-w-0 ${check.met ? 'text-neutral-400' : 'text-neutral-200'}`}>
                      <span className="font-mono">{check.name}</span>
                      {!check.met && check.fix_hint && (
                        <span className="ml-1 text-amber-400/80">{check.fix_hint}</span>
                      )}
                      {!check.met && (
                        <span className="ml-1 text-neutral-500">({check.blocking_value})</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {shadowHealth && shadowHealth.shadow_lane_count > 0 && (
            <div
              data-testid="shadow-lane-health"
              className="mb-3 rounded border border-orange-500/20 bg-orange-500/10 p-2 text-xs"
            >
              <div className="mb-1 font-semibold text-orange-300">Shadow checkpoint health</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-400 sm:grid-cols-4">
                <div>
                  <span className="text-neutral-500">Lanes </span>
                  <span className="text-neutral-200">{shadowHealth.shadow_lane_count}</span>
                </div>
                <div>
                  <span className="text-neutral-500">Written </span>
                  <span className="text-neutral-200">{shadowHealth.checkpoints_written}</span>
                </div>
                <div>
                  <span className="text-neutral-500">Misses </span>
                  <span className={shadowHealth.checkpoint_miss_count > 0 ? 'text-amber-300' : 'text-neutral-200'}>
                    {shadowHealth.checkpoint_miss_count}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Miss rate </span>
                  <span className={shadowHealth.miss_rate != null && shadowHealth.miss_rate > 0 ? 'text-amber-300' : 'text-neutral-200'}>
                    {shadowHealth.miss_rate != null ? `${(shadowHealth.miss_rate * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                {shadowHealth.exhausted_close_count > 0 && (
                  <div className="col-span-2 sm:col-span-4">
                    <span className="text-neutral-500">Exhausted closes </span>
                    <span className="text-red-300">{shadowHealth.exhausted_close_count}</span>
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="mb-2 flex items-center justify-between gap-2">
            <button
              type="button"
              data-testid="drain-lane-pilot-button"
              disabled={draining || !projectId}
              onClick={() => setShowDrainConfirm(true)}
              className="min-h-[44px] rounded border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-[11px] font-semibold text-red-400 transition-colors hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {draining ? 'Draining…' : 'Drain / Kill Pilot'}
            </button>
            {drainResult && (
              <span className="text-[11px] text-emerald-400" data-testid="drain-result-summary">
                Pilot drained: {drainResult.reservations_cleared} reservation{drainResult.reservations_cleared !== 1 ? 's' : ''} released, {drainResult.lanes_killed} lane{drainResult.lanes_killed !== 1 ? 's' : ''} killed
              </span>
            )}
          </div>

          {drainError && (
            <div className="mb-2 flex items-start gap-2 rounded border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-300">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="min-w-0 break-words">{drainError}</span>
            </div>
          )}

          {fetchError && (
            <div className="flex items-start gap-2 rounded border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-300">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="min-w-0 break-words">{fetchError}</span>
            </div>
          )}

          {loading && !fetchError && lanes.length === 0 && (
            <div className="flex items-center gap-2 px-1 py-2 text-neutral-500">
              <RefreshCw className="h-3 w-3 animate-spin" />
              Loading improvement lanes…
            </div>
          )}

          {!loading && !fetchError && lanes.length === 0 && (
            <div className="rounded border border-neutral-800/80 bg-neutral-950/60 px-3 py-2 text-neutral-400">
              No improvement lanes for this project yet.
            </div>
          )}

          {hasLanes && (
            <div className="space-y-1.5">
              {lanes.map((lane) => {
                const reservationExpiresAt = lane.reservation_expires_at
                  ? formatAbsolute(parseTimestamp(lane.reservation_expires_at).getTime())
                  : '—';
                return (
                  <div
                    key={lane.id}
                    data-testid="improvement-lane-row"
                    className={`rounded-lg border bg-neutral-950/60 ${lane.shadow ? 'border-orange-500/20' : 'border-neutral-800/80'}`}
                  >
                    <div className="flex min-h-[44px] flex-wrap items-center gap-1.5 px-3 py-2">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${statusClasses(lane.status)}`}>
                        {lane.status}
                      </span>
                      {lane.shadow && (
                        <span
                          data-testid="improvement-lane-shadow-badge"
                          className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none ${shadowClasses(true)}`}
                        >
                          Shadow mode
                        </span>
                      )}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium text-neutral-200">
                          {lane.branch ?? '—'}
                        </span>
                        <span className="block truncate font-mono text-[10px] text-neutral-500">
                          {lane.direction_id ?? '—'}
                        </span>
                      </span>
                      <span className="shrink-0 text-[10px] text-neutral-500">
                        {formatAgeSeconds(lane.last_checkpoint_age_seconds)}
                      </span>
                    </div>

                    <div className="grid grid-cols-1 gap-2 border-t border-neutral-800/80 px-3 py-2 sm:grid-cols-2 xl:grid-cols-4">
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Checkpoint</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          <span className="font-medium">{lane.last_checkpoint_phase ?? '—'}</span>
                          <span className="text-neutral-600"> · </span>
                          <span className="font-mono">{lane.last_checkpoint_seq ?? '—'}</span>
                          <span className="text-neutral-600"> · </span>
                          <span className="font-mono">{lane.last_checkpoint_sha ?? '—'}</span>
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          Age {formatAgeSeconds(lane.last_checkpoint_age_seconds)}
                        </div>
                      </div>

                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Workspace</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.workspace_state ?? '—'}
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          {lane.workspace_vm_instance_id ?? '—'}
                        </div>
                      </div>

                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Reservation</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.reserved_vm_instance_id ?? '—'}
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          Expires {reservationExpiresAt}
                        </div>
                        <div className="mt-1 space-y-0.5 text-[10px]" data-testid="reservation-cause-breakdown">
                          <div>
                            <span className="text-neutral-500">Claimed </span>
                            <span className={lane.reservation_claimed_by_fix_count != null && lane.reservation_claimed_by_fix_count > 0 ? 'font-medium text-emerald-400' : 'text-neutral-400'}>
                              {lane.reservation_claimed_by_fix_count ?? '—'}
                            </span>
                          </div>
                          <div>
                            <span className="text-neutral-500">Expired early </span>
                            <span className={lane.reservation_expired_before_fix_count != null && lane.reservation_expired_before_fix_count > 0 ? 'font-medium text-amber-400' : 'text-neutral-400'}>
                              {lane.reservation_expired_before_fix_count ?? '—'}
                            </span>
                          </div>
                          <div>
                            <span className="text-neutral-500">Force-expired </span>
                            <span className={lane.reservation_force_expired_count != null && lane.reservation_force_expired_count > 0 ? 'font-medium text-red-400' : 'text-neutral-400'}>
                              {lane.reservation_force_expired_count ?? '—'}
                            </span>
                          </div>
                        </div>
                      </div>

                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Pilot / requeues</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.pilot_arm ?? '—'}
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          Phase requeues {lane.phase_requeues}
                        </div>
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2 border-t border-neutral-800/80 px-3 py-2 text-neutral-300 sm:grid-cols-4">
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">LLM input</div>
                        <div className="mt-0.5">{lane.llm_input_tokens_total ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">LLM output</div>
                        <div className="mt-0.5">{lane.llm_output_tokens_total ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">VM cost</div>
                        <div className="mt-0.5 inline-flex items-center gap-1">
                          <CircleDollarSign className="h-3 w-3 shrink-0 text-neutral-500" />
                          {formatUsd(lane.vm_cost_usd_total)}
                        </div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Reserved idle / loss</div>
                        <div className="mt-0.5 inline-flex items-center gap-1">
                          <TimerReset className="h-3 w-3 shrink-0 text-neutral-500" />
                          {formatUsd(lane.reserved_idle_cost_usd_total)} / {lane.checkpoint_loss_count}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ImprovementLanesPanel;
