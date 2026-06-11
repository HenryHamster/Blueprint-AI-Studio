import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight, CircleDollarSign, GitBranch, RefreshCw, TimerReset } from 'lucide-react';
import { fetchImprovementLanes, type ImprovementLaneRow } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute, formatDurationSec, parseTimestamp } from '../utils/time';

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
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsedPref, setCollapsedPref] = useState<boolean | null>(() => readCollapsedPreference());

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setLoading(true);
    setFetchError(null);
    try {
      const data = await fetchImprovementLanes(projectId, 50, 0, signal);
      if (signal?.aborted) return;
      setLanes(data.lanes ?? []);
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      setLanes([]);
      setFetchError(e instanceof Error ? e.message : 'Failed to load improvement lanes');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLanes([]);
    setFetchError(null);
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
        {collapsed && fetchError && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-[10px] font-bold leading-none text-red-300">
            Error
          </span>
        )}
      </button>

      {!collapsed && (
        <div className="mt-1 rounded-lg border border-neutral-800 bg-neutral-900 p-3 text-xs">
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
