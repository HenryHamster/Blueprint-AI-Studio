/* eslint-disable react-hooks/purity, react-hooks/set-state-in-effect */
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle, ChevronDown, ChevronRight, CircleDollarSign, GitBranch, RefreshCw, TimerReset, XCircle } from 'lucide-react';
import { drainLanePilot, fetchImprovementLaneDetail, fetchImprovementLanes, fetchLanePilotPreflight, reclaimOrphanedLaneWorkspaces, type ImprovementLaneCheckpoint, type ImprovementLaneCurrentTask, type ImprovementLaneDetail, type ImprovementLaneManifest, type ImprovementLaneRow, type LanePilotDrainReport, type LanePilotPreflightResponse, type LanePilotTrialMetrics, type ShadowLaneHealth } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { formatAbsolute, formatDurationSec, parseTimestamp, timeAgo } from '../utils/time';
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

function formatCheckpointAge(sha: string | null | undefined, seconds: number | null | undefined): string {
  if (sha == null) return 'none';
  return formatAgeSeconds(seconds);
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
      return 'border-neutral-700 bg-workbench-panel text-neutral-300';
  }
}

function shadowClasses(shadow: boolean): string {
  return shadow
    ? 'border-orange-500/30 bg-orange-500/15 text-orange-300'
    : 'border-neutral-700 bg-workbench-panel text-neutral-300';
}

function CheckpointRow({ cp }: { cp: ImprovementLaneCheckpoint }) {
  const short = (sha: string | null | undefined) => sha ? sha.slice(0, 7) : '—';
  const boolBadge = (v: boolean | null | undefined, trueLabel = '✓', falseLabel = '✗') => {
    if (v == null) return <span className="text-neutral-600">—</span>;
    return v
      ? <span className="text-emerald-400">{trueLabel}</span>
      : <span className="text-red-400">{falseLabel}</span>;
  };
  return (
    <tr className="border-t border-neutral-800/60">
      <td className="py-1 pr-3 font-mono text-ui-sm text-neutral-500">{cp.seq}</td>
      <td className="py-1 pr-3 font-mono text-ui-sm text-neutral-300">{cp.phase}</td>
      <td className="py-1 pr-3 font-mono text-ui-sm text-neutral-400">{short(cp.sha)}</td>
      <td className="py-1 pr-3 text-center">{boolBadge(cp.validated?.lint)}</td>
      <td className="py-1 pr-3 text-center">{boolBadge(cp.validated?.tests)}</td>
      <td className="py-1 pr-3 text-center">{cp.warm_resume == null ? <span className="text-neutral-600">—</span> : cp.warm_resume ? <span className="text-emerald-400">warm</span> : <span className="text-neutral-400">cold</span>}</td>
      <td className="py-1 pr-3 text-ui-sm text-neutral-400">{cp.tokens_in != null ? cp.tokens_in.toLocaleString() : '—'}</td>
      <td className="py-1 text-ui-sm text-neutral-400">{cp.vm_cost_usd != null ? `$${cp.vm_cost_usd.toFixed(4)}` : '—'}</td>
    </tr>
  );
}

function ManifestSection({ manifest }: { manifest: ImprovementLaneManifest }) {
  const listItems = (items: unknown[] | null | undefined) =>
    items && items.length > 0
      ? <ul className="mt-0.5 space-y-0.5">{items.map((it, i) => <li key={i} className="font-mono text-ui-sm text-neutral-300">{String(it)}</li>)}</ul>
      : <span className="text-ui-sm text-neutral-600">none</span>;

  const hasContent =
    manifest.progress != null ||
    (manifest.phase_log && manifest.phase_log.length > 0) ||
    (manifest.remaining && (manifest.remaining as unknown[]).length > 0) ||
    (manifest.critical_files && manifest.critical_files.length > 0) ||
    (manifest.failing_tests && manifest.failing_tests.length > 0) ||
    (manifest.risks && manifest.risks.length > 0) ||
    manifest.recovery != null;

  if (!hasContent) {
    return <div className="text-ui-sm text-neutral-600">No manifest data</div>;
  }

  return (
    <div className="space-y-2">
      {manifest.progress != null && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Progress</div>
          <div className="mt-0.5 text-ui-sm text-neutral-300">{String(manifest.progress)}</div>
        </div>
      )}
      {manifest.phase_log && manifest.phase_log.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Phase log</div>
          <div className="mt-0.5 space-y-0.5">
            {manifest.phase_log.map((entry, i) => (
              <div key={i} className="font-mono text-ui-sm text-neutral-300">
                {typeof entry === 'object' ? JSON.stringify(entry) : String(entry)}
              </div>
            ))}
          </div>
        </div>
      )}
      {(manifest.remaining as unknown[] | null | undefined) && (manifest.remaining as unknown[]).length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Remaining</div>
          {listItems(manifest.remaining as unknown[])}
        </div>
      )}
      {manifest.critical_files && manifest.critical_files.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Critical files</div>
          {listItems(manifest.critical_files)}
        </div>
      )}
      {manifest.failing_tests && manifest.failing_tests.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Failing tests</div>
          {listItems(manifest.failing_tests)}
        </div>
      )}
      {manifest.risks && manifest.risks.length > 0 && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Risks</div>
          {listItems(manifest.risks)}
        </div>
      )}
      {manifest.recovery != null && (
        <div>
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Recovery</div>
          <div className="mt-0.5 font-mono text-ui-sm text-neutral-300">
            {typeof manifest.recovery === 'object' ? JSON.stringify(manifest.recovery) : String(manifest.recovery)}
          </div>
        </div>
      )}
    </div>
  );
}

function CurrentTaskSection({ task }: { task: ImprovementLaneCurrentTask | null }) {
  if (task == null) {
    return <div className="text-ui-sm text-neutral-600">No task currently running</div>;
  }
  const statusColors: Record<string, string> = {
    IN_PROGRESS: 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300',
    PENDING: 'border-amber-500/30 bg-amber-500/15 text-amber-300',
    COMPLETED: 'border-sky-500/30 bg-sky-500/15 text-sky-300',
    FAILED: 'border-red-500/30 bg-red-500/15 text-red-300',
  };
  const statusClass = statusColors[task.status?.toUpperCase()] ?? 'border-neutral-700 bg-workbench-panel text-neutral-300';
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className={`rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${statusClass}`}>
        {task.status}
      </span>
      {task.task_type && (
        <span className="rounded border border-neutral-700 bg-workbench-panel px-1.5 py-0.5 text-ui-sm leading-none text-neutral-400">
          {task.task_type}
        </span>
      )}
      <span className="text-ui-sm text-neutral-300">{task.title ?? '—'}</span>
      <span className="font-mono text-ui-sm text-neutral-600">{task.task_id}</span>
    </div>
  );
}

interface LaneDetailPanelProps {
  projectId: string;
  laneId: string;
}

function LaneDetailPanel({ projectId, laneId }: LaneDetailPanelProps) {
  const [detail, setDetail] = useState<ImprovementLaneDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fetchedForRef = useRef<string | null>(null);

  useEffect(() => {
    const key = `${projectId}:${laneId}`;
    if (fetchedForRef.current === key && detail != null) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchImprovementLaneDetail(projectId, laneId, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        fetchedForRef.current = key;
        setDetail(data);
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setError(e instanceof Error ? e.message : 'Failed to load lane detail');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, laneId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-3 py-3 text-neutral-500">
        <RefreshCw className="h-3 w-3 animate-spin" />
        <span className="text-ui-md">Loading detail…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-2 px-3 py-2 text-red-300">
        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
        <span className="text-ui-md">{error}</span>
      </div>
    );
  }

  if (!detail) return null;

  const checkpoints = [...(detail.checkpoints ?? [])].reverse();

  return (
    <div className="border-t border-neutral-800/80 bg-workbench-shell/40 px-3 py-3 space-y-4">
      {/* Current task */}
      <div>
        <div className="mb-1.5 text-ui-sm uppercase tracking-wide text-neutral-500 font-semibold">Current task</div>
        <CurrentTaskSection task={detail.current_task} />
      </div>

      {/* Checkpoint history */}
      <div>
        <div className="mb-1.5 text-ui-sm uppercase tracking-wide text-neutral-500 font-semibold">
          Checkpoint history ({checkpoints.length})
        </div>
        {checkpoints.length === 0 ? (
          <div className="text-ui-sm text-neutral-600">No checkpoints yet</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-ui-md">
              <thead>
                <tr className="text-ui-sm uppercase tracking-wide text-neutral-600">
                  <th className="pb-1 pr-3 font-medium">#</th>
                  <th className="pb-1 pr-3 font-medium">Phase</th>
                  <th className="pb-1 pr-3 font-medium">SHA</th>
                  <th className="pb-1 pr-3 font-medium">Lint</th>
                  <th className="pb-1 pr-3 font-medium">Tests</th>
                  <th className="pb-1 pr-3 font-medium">Resume</th>
                  <th className="pb-1 pr-3 font-medium">Tokens in</th>
                  <th className="pb-1 font-medium">VM cost</th>
                </tr>
              </thead>
              <tbody>
                {checkpoints.map((cp) => <CheckpointRow key={cp.seq} cp={cp} />)}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Manifest */}
      <div>
        <div className="mb-1.5 text-ui-sm uppercase tracking-wide text-neutral-500 font-semibold">Manifest</div>
        <ManifestSection manifest={detail.manifest} />
      </div>
    </div>
  );
}

export function ImprovementLanesPanel({ projectId }: ImprovementLanesPanelProps) {
  const [lanes, setLanes] = useState<ImprovementLaneRow[]>([]);
  const [shadowHealth, setShadowHealth] = useState<ShadowLaneHealth | null>(null);
  const [trialMetrics, setTrialMetrics] = useState<LanePilotTrialMetrics | null>(null);
  const [workspaceStateCounts, setWorkspaceStateCounts] = useState<Record<string, number> | null>(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [collapsedPref, setCollapsedPref] = useState<boolean | null>(() => readCollapsedPreference());
  const [expandedLaneId, setExpandedLaneId] = useState<string | null>(null);
  const [showDrainConfirm, setShowDrainConfirm] = useState(false);
  const [draining, setDraining] = useState(false);
  const [drainResult, setDrainResult] = useState<LanePilotDrainReport | null>(null);
  const [drainError, setDrainError] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<LanePilotPreflightResponse | null>(null);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [reclaiming, setReclaiming] = useState(false);
  const [reclaimResult, setReclaimResult] = useState<number | null>(null);
  const [reclaimError, setReclaimError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!projectId) return;
    setLoading(true);
    setFetchError(null);
    setPreflightError(null);
    try {
      const [lanesData, preflightData] = await Promise.all([
        fetchImprovementLanes(projectId, 50, 0, signal),
        fetchLanePilotPreflight(projectId, signal).catch((e) => {
          if (signal?.aborted) return null;
          const errorMsg = e instanceof Error ? e.message : 'Failed to fetch preflight data';
          setPreflightError(errorMsg);
          return null;
        }),
      ]);
      if (signal?.aborted) return;
      setLanes(lanesData.lanes ?? []);
      setShadowHealth(lanesData.shadow_health ?? null);
      setTrialMetrics(lanesData.trial_metrics ?? null);
      setWorkspaceStateCounts(lanesData.workspace_state_counts ?? null);
      setPreflight(preflightData);
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      setLanes([]);
      setFetchError(e instanceof Error ? e.message : 'Failed to load improvement lanes');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [projectId]);

  const handleReclaimOrphaned = useCallback(async () => {
    if (!projectId) return;
    setReclaiming(true);
    setReclaimError(null);
    setReclaimResult(null);
    try {
      const result = await reclaimOrphanedLaneWorkspaces(projectId);
      setReclaimResult(result.reclaimed);
      await load();
    } catch (e) {
      setReclaimError(e instanceof Error ? e.message : 'Failed to reclaim orphaned workspaces');
    } finally {
      setReclaiming(false);
    }
  }, [projectId, load]);

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
    setTrialMetrics(null);
    setFetchError(null);
    setPreflight(null);
    setPreflightError(null);
    setLoading(Boolean(projectId));
    setDrainResult(null);
    setDrainError(null);
    setReclaimResult(null);
    setReclaimError(null);
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
        className="flex flex-wrap min-h-[44px] w-full items-center gap-2 px-1 py-1 text-ui-md font-bold uppercase tracking-widest text-neutral-400 transition-colors hover:text-neutral-300"
      >
        {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        <GitBranch className="h-3 w-3" />
        Improvement lanes
        {hasLanes && (
          <span className="ml-1 rounded-full border border-neutral-700 bg-workbench-panel px-1.5 py-0.5 text-ui-sm font-bold leading-none text-neutral-300">
            {lanes.length} lanes
          </span>
        )}
        {activeCount > 0 && (
          <span className="rounded-full border border-emerald-500/30 bg-emerald-500/15 px-1.5 py-0.5 text-ui-sm font-bold leading-none text-emerald-300">
            {activeCount} active
          </span>
        )}
        {shadowCount > 0 && (
          <span
            data-testid="improvement-lanes-shadow-summary"
            className="rounded-full border border-orange-500/30 bg-orange-500/15 px-1.5 py-0.5 text-ui-sm font-bold leading-none text-orange-300"
          >
            {shadowCount} shadow
          </span>
        )}
        {shadowHealth && shadowHealth.shadow_lane_count > 0 && shadowHealth.miss_rate != null && (
          <span
            data-testid="improvement-lanes-shadow-miss-rate"
            className={`rounded-full border px-1.5 py-0.5 text-ui-sm font-bold leading-none ${shadowHealth.miss_rate > 0 ? 'border-amber-500/30 bg-amber-500/15 text-amber-300' : 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300'}`}
          >
            miss {(shadowHealth.miss_rate * 100).toFixed(0)}%
          </span>
        )}
        {collapsed && fetchError && (
          <span className="ml-1 rounded-full border border-red-500/30 bg-red-500/15 px-1.5 py-0.5 text-ui-sm font-bold leading-none text-red-300">
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
        <div className="mt-1 rounded-lg border border-neutral-800 bg-workbench-base p-3 text-xs">
          {preflightError && (
            <div className="mb-3 flex items-start gap-2 rounded border border-amber-500/20 bg-amber-500/10 px-3 py-2">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber-400" />
              <span className="min-w-0 break-words text-amber-300 text-ui-md">Preflight data unavailable</span>
            </div>
          )}
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
                  <span className={`rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${preflight.pilot_mode === 'on' ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : 'border-amber-500/40 bg-amber-500/15 text-amber-300'}`}>
                    {preflight.pilot_mode === 'on' ? 'pilot on' : 'shadow'}
                  </span>
                )}
                {preflight.durable_verdict && (
                  <span className={`ml-auto rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${preflight.durable_verdict === 'KILL' ? 'border-red-500/40 bg-red-500/15 text-red-300' : 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'}`}>
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
          {trialMetrics && (
            <div
              data-testid="lane-pilot-trial-metrics"
              className="mb-3 rounded border border-neutral-700 bg-workbench-shell/60 p-2 text-xs"
            >
              <div className="mb-1.5 flex items-center gap-1.5">
                <span className="font-semibold text-neutral-200">Trial metrics</span>
                <span className={`ml-auto rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${trialMetrics.verdict === 'KILL' ? 'border-red-500/40 bg-red-500/15 text-red-300' : trialMetrics.verdict === 'ADOPT' ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : 'border-neutral-700 bg-workbench-panel text-neutral-300'}`}>
                  {trialMetrics.verdict}
                </span>
                {trialMetrics.immediate_alert && (
                  <span className="rounded border border-red-500/40 bg-red-500/15 px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none text-red-300">
                    Alert
                  </span>
                )}
              </div>
              <div className="mb-1.5 text-ui-sm text-neutral-500">
                Reason: <span className="font-mono text-neutral-300">{trialMetrics.verdict_reason}</span>
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-400 sm:grid-cols-4">
                <div>
                  <span className="text-neutral-500">Lane n </span>
                  <span className="text-neutral-200">{trialMetrics.n_lane}</span>
                </div>
                <div>
                  <span className="text-neutral-500">Control n </span>
                  <span className="text-neutral-200">{trialMetrics.n_control}</span>
                </div>
                <div>
                  <span className="text-neutral-500">Lane merge </span>
                  <span className="text-neutral-200">
                    {trialMetrics.lane_merge_rate != null ? `${(trialMetrics.lane_merge_rate * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Control merge </span>
                  <span className="text-neutral-200">
                    {trialMetrics.control_merge_rate != null ? `${(trialMetrics.control_merge_rate * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Lane $/merged </span>
                  <span className="text-neutral-200">
                    {trialMetrics.lane_cost_per_merged != null ? formatUsd(trialMetrics.lane_cost_per_merged) : '—'}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Control $/merged </span>
                  <span className="text-neutral-200">
                    {trialMetrics.control_cost_per_merged != null ? formatUsd(trialMetrics.control_cost_per_merged) : '—'}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Checkpoint loss </span>
                  <span className={trialMetrics.checkpoint_loss_rate != null && trialMetrics.checkpoint_loss_rate > 0.05 ? 'text-red-300' : 'text-neutral-200'}>
                    {trialMetrics.checkpoint_loss_rate != null ? `${(trialMetrics.checkpoint_loss_rate * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div>
                  <span className="text-neutral-500">Warm resume </span>
                  <span className="text-neutral-200">
                    {trialMetrics.warm_resume_fraction != null ? `${(trialMetrics.warm_resume_fraction * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                {trialMetrics.gate_integrity_violations > 0 && (
                  <div className="col-span-2 sm:col-span-4">
                    <span className="text-neutral-500">Gate violations </span>
                    <span className="text-red-300">{trialMetrics.gate_integrity_violations}</span>
                  </div>
                )}
              </div>
              {trialMetrics.lane_relay_comparison && (() => {
                const cmp = trialMetrics.lane_relay_comparison;
                const fmtSec = (v: number | null) => v != null ? `${v.toFixed(0)}s` : '—';
                const fmtDelta = (v: number | null) => {
                  if (v == null) return '—';
                  const sign = v < 0 ? '' : '+';
                  return `${sign}${v.toFixed(0)}s`;
                };
                return (
                  <div className="mt-2 border-t border-neutral-700/60 pt-2">
                    <div className="mb-1 text-ui-sm text-neutral-500">
                      Relay comparison vs v{cmp.source_version.version_number}
                    </div>
                    <div className="grid grid-cols-4 gap-x-4 gap-y-0.5 text-ui-sm text-neutral-400">
                      <div className="text-neutral-600" />
                      <div className="text-neutral-500">Wall clock</div>
                      <div className="text-neutral-500">Active</div>
                      <div className="text-neutral-500">Gap</div>
                      <div className="text-neutral-500">Relay</div>
                      <div className="text-neutral-300">{fmtSec(cmp.relay.wall_clock_seconds)}</div>
                      <div className="text-neutral-300">{fmtSec(cmp.relay.active_seconds)}</div>
                      <div className="text-neutral-300">{fmtSec(cmp.relay.gap_seconds)}</div>
                      <div className="text-neutral-500">Lane</div>
                      <div className="text-neutral-300">{fmtSec(cmp.lane.wall_clock_seconds)}</div>
                      <div className="text-neutral-300">{fmtSec(cmp.lane.active_seconds)}</div>
                      <div className="text-neutral-300">{fmtSec(cmp.lane.gap_seconds)}</div>
                      <div className="text-neutral-500">Delta</div>
                      <div className={cmp.deltas.wall_clock_seconds != null && cmp.deltas.wall_clock_seconds < 0 ? 'text-emerald-400' : 'text-neutral-300'}>{fmtDelta(cmp.deltas.wall_clock_seconds)}</div>
                      <div className={cmp.deltas.active_seconds != null && cmp.deltas.active_seconds < 0 ? 'text-emerald-400' : 'text-neutral-300'}>{fmtDelta(cmp.deltas.active_seconds)}</div>
                      <div className={cmp.deltas.gap_seconds != null && cmp.deltas.gap_seconds < 0 ? 'text-emerald-400' : 'text-neutral-300'}>{fmtDelta(cmp.deltas.gap_seconds)}</div>
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
          {workspaceStateCounts && Object.keys(workspaceStateCounts).length > 0 && (
            <div
              data-testid="workspace-state-counts"
              className="mb-3 rounded border border-neutral-700 bg-workbench-panel/50 p-2 text-xs"
            >
              <div className="mb-1 font-semibold text-neutral-300">Workspace states (fleet)</div>
              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-neutral-400">
                {Object.entries(workspaceStateCounts).sort(([a], [b]) => a.localeCompare(b)).map(([state, count]) => (
                  <div key={state}>
                    <span className="text-neutral-500">{state} </span>
                    <span className={state === 'WARM_RESERVED' || state === 'LEASED' ? 'text-emerald-300' : count > 0 && state !== 'COLD' ? 'text-amber-300' : 'text-neutral-200'}>
                      {count}
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

          <div className="mb-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="drain-lane-pilot-button"
              disabled={draining || !projectId}
              onClick={() => setShowDrainConfirm(true)}
              className="min-h-[44px] rounded border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-ui-md font-semibold text-red-400 transition-colors hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {draining ? 'Draining…' : 'Drain / Kill Pilot'}
            </button>
            <button
              type="button"
              data-testid="reclaim-orphaned-button"
              disabled={reclaiming || !projectId}
              onClick={handleReclaimOrphaned}
              className="min-h-[44px] rounded border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-ui-md font-semibold text-amber-400 transition-colors hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {reclaiming ? 'Reclaiming…' : 'Reclaim Orphaned'}
            </button>
            {drainResult && (
              <span className="text-ui-md text-emerald-400" data-testid="drain-result-summary">
                Pilot drained: {drainResult.reservations_cleared} reservation{drainResult.reservations_cleared !== 1 ? 's' : ''} released, {drainResult.lanes_killed} lane{drainResult.lanes_killed !== 1 ? 's' : ''} killed
              </span>
            )}
            {reclaimResult != null && (
              <span className="text-ui-md text-emerald-400" data-testid="reclaim-result-summary">
                Reclaimed {reclaimResult} orphaned lane{reclaimResult !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          {reclaimError && (
            <div className="mb-2 flex items-start gap-2 rounded border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-amber-300">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="min-w-0 break-words" data-testid="reclaim-error">{reclaimError}</span>
            </div>
          )}

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
            <div className="rounded border border-neutral-800/80 bg-workbench-shell/60 px-3 py-2 text-neutral-400">
              No improvement lanes for this project yet.
            </div>
          )}

          {hasLanes && (
            <div className="space-y-1.5">
              {lanes.map((lane) => {
                const reservationExpiresAt = lane.reservation_expires_at
                  ? formatAbsolute(parseTimestamp(lane.reservation_expires_at).getTime())
                  : '—';
                const reservationIsExpired =
                  lane.reservation_expires_at != null &&
                  parseTimestamp(lane.reservation_expires_at).getTime() < Date.now();
                const isExpanded = expandedLaneId === lane.id;
                return (
                  <div
                    key={lane.id}
                    data-testid="improvement-lane-row"
                    className={`rounded-lg border bg-workbench-shell/60 ${lane.shadow ? 'border-orange-500/20' : 'border-neutral-800/80'}`}
                  >
                    <div className="flex min-h-[44px] flex-wrap items-center gap-1.5 px-3 py-2">
                      <button
                        type="button"
                        data-testid="lane-expand-toggle"
                        aria-expanded={isExpanded}
                        onClick={() => setExpandedLaneId(isExpanded ? null : lane.id)}
                        className="min-h-[44px] min-w-[44px] shrink-0 flex items-center justify-center rounded text-neutral-500 transition-colors hover:bg-workbench-hover hover:text-neutral-300"
                      >
                        {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      </button>
                      <span className={`rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${statusClasses(lane.status)}`}>
                        {lane.status}
                      </span>
                      {lane.shadow && (
                        <span
                          data-testid="improvement-lane-shadow-badge"
                          className={`rounded border px-1.5 py-0.5 text-ui-sm font-bold uppercase leading-none ${shadowClasses(true)}`}
                        >
                          Shadow mode
                        </span>
                      )}
                      <span className="min-w-0 flex-1">
                        {lane.title && (
                          <span className="block truncate font-medium text-neutral-200">
                            {lane.title}
                          </span>
                        )}
                        <span className={`block truncate ${lane.title ? 'font-mono text-ui-sm text-neutral-500' : 'font-medium text-neutral-200'}`}>
                          {lane.branch ?? '—'}
                        </span>
                        <span className="block truncate font-mono text-ui-sm text-neutral-500">
                          {lane.direction_id ?? '—'}
                        </span>
                      </span>
                      <span className="shrink-0 text-ui-sm text-neutral-500">
                        {formatCheckpointAge(lane.last_checkpoint_sha, lane.last_checkpoint_age_seconds)}
                      </span>
                    </div>

                    <div className="grid grid-cols-1 gap-2 border-t border-neutral-800/80 px-3 py-2 sm:grid-cols-2 xl:grid-cols-4">
                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Checkpoint</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          <span className="font-medium">{lane.last_checkpoint_phase ?? '—'}</span>
                          <span className="text-neutral-600"> · </span>
                          <span className="font-mono">{lane.last_checkpoint_seq ?? '—'}</span>
                          <span className="text-neutral-600"> · </span>
                          <span className="font-mono">{lane.last_checkpoint_sha ?? '—'}</span>
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          Age {formatCheckpointAge(lane.last_checkpoint_sha, lane.last_checkpoint_age_seconds)}
                        </div>
                      </div>

                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Workspace</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.workspace_state ?? '—'}
                        </div>
                        <div className="mt-0.5 break-words text-neutral-500">
                          {lane.workspace_vm_instance_id ?? '—'}
                        </div>
                      </div>

                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Reservation</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.reserved_vm_instance_id ?? '—'}
                        </div>
                        {reservationIsExpired ? (
                          <div className="mt-0.5 text-amber-400" data-testid="reservation-expiry">
                            Expired {timeAgo(lane.reservation_expires_at as string)}
                          </div>
                        ) : (
                          <div className="mt-0.5 text-neutral-500" data-testid="reservation-expiry">
                            Expires {reservationExpiresAt}
                          </div>
                        )}
                        <div className="mt-1 space-y-0.5 text-ui-sm" data-testid="reservation-cause-breakdown">
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
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Pilot / requeues</div>
                        <div className="mt-0.5 break-words text-neutral-300">
                          {lane.pilot_arm ?? '—'}
                        </div>
                        <div className="mt-0.5 text-neutral-500">
                          Phase requeues {lane.phase_requeues}
                        </div>
                        {lane.recovery && (
                          <div className="mt-1 space-y-0.5 text-ui-sm" data-testid="lane-recovery">
                            <div className={lane.recovery.exhausted ? 'font-medium text-red-400' : 'text-amber-400'}>
                              Recovery #{lane.recovery.attempt}{lane.recovery.exhausted ? ' (exhausted)' : ''}
                            </div>
                            <div className="text-neutral-500">{lane.recovery.reason}</div>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2 border-t border-neutral-800/80 px-3 py-2 text-neutral-300 sm:grid-cols-4">
                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">LLM input</div>
                        <div className="mt-0.5">{lane.llm_input_tokens_total ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">LLM output</div>
                        <div className="mt-0.5">{lane.llm_output_tokens_total ?? '—'}</div>
                      </div>
                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">VM cost</div>
                        <div className="mt-0.5 inline-flex items-center gap-1">
                          <CircleDollarSign className="h-3 w-3 shrink-0 text-neutral-500" />
                          {formatUsd(lane.vm_cost_usd_total)}
                        </div>
                      </div>
                      <div>
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Reserved idle / loss</div>
                        <div className="mt-0.5 inline-flex items-center gap-1">
                          <TimerReset className="h-3 w-3 shrink-0 text-neutral-500" />
                          {formatUsd(lane.reserved_idle_cost_usd_total)} / {lane.checkpoint_loss_count}
                        </div>
                      </div>
                    </div>
                    {isExpanded && projectId && (
                      <LaneDetailPanel projectId={projectId} laneId={lane.id} />
                    )}
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
