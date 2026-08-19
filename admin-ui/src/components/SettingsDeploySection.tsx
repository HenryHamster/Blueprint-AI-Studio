import { useEffect, useState } from 'react';
import { Play, Pause, RotateCw, Wrench, CheckCircle, Zap, AlertTriangle } from 'lucide-react';
import { parseTimestamp } from '../utils/time';
import { apiFetch } from '../api';
import type { DeployState, FailedRollbackInfo } from '../types/settings';

function MergeGradeGateSettings() {
  const [threshold, setThreshold] = useState<number>(0.0);
  const [input, setInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveOk, setSaveOk] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const res = await apiFetch('/api/admin/settings');
        if (res.ok) {
          const data = await res.json();
          const val = data?.merge_grade_block_threshold ?? 0.0;
          setThreshold(val);
          setInput(String(val));
        }
      } catch {
        // Unauthenticated / network error on mount: keep defaults. Mirrors the
        // defensive mount-fetch handling in sibling settings panels so a failed
        // load never escapes as an unhandled promise rejection (which fails the
        // viewport browser-mode test run even when every test file passes).
      }
    })();
  }, []);

  const save = async () => {
    const val = parseFloat(input);
    if (isNaN(val) || val < 0 || val > 1) { setSaveError('Must be a number between 0.0 and 1.0'); return; }
    setSaving(true); setSaveError(null); setSaveOk(false);
    try {
      const res = await apiFetch('/api/admin/settings', { method: 'PATCH', body: JSON.stringify({ merge_grade_block_threshold: val }) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d?.detail || `HTTP ${res.status}`); }
      setThreshold(val); setSaveOk(true); setTimeout(() => setSaveOk(false), 2000);
    } catch (e) { setSaveError((e as Error).message); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-300">Release Gate Thresholds</h3>
      <p className="mb-3 text-ui-md text-neutral-500">
        Set <code className="font-mono">merge_grade_block_threshold</code> from <code className="font-mono">0.0</code> to <code className="font-mono">1.0</code>. A value of <code className="font-mono">0.0</code> leaves the gate disabled.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-neutral-400 w-48">merge_grade_block_threshold</label>
        <input
          type="number" min={0} max={1} step={0.1}
          value={input} onChange={e => setInput(e.target.value)}
          className="w-24 bg-workbench-shell border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-sm font-mono workbench-focus"
          data-testid="merge-grade-block-threshold-input"
        />
        <button
          onClick={save} disabled={saving}
          className="inline-flex min-h-[44px] items-center justify-center rounded-md border border-neutral-700 bg-workbench-hover px-3 py-1 text-xs text-neutral-300 transition-colors hover:bg-workbench-hover disabled:opacity-50"
          data-testid="merge-grade-block-threshold-save"
        >
          {saving ? 'Saving…' : saveOk ? 'Saved' : 'Save'}
        </button>
        <span className="text-ui-md text-neutral-500">current: <span className="font-mono text-neutral-300">{threshold}</span></span>
      </div>
      {saveError && <p className="text-xs text-red-400 mt-1">{saveError}</p>}
    </div>
  );
}

const EXTERNAL_DEP_ORDER = ['github_api', 'db', 'dispatch_api', 'anthropic_api', 'zai_api', 'deepseek_api', 'openai_api', 'gemini_api', 'gcp'] as const;

const EXTERNAL_DEP_LABELS: Record<string, string> = {
  github_api: 'GitHub',
  db: 'DB',
  dispatch_api: 'Dispatch',
  anthropic_api: 'Anthropic',
  zai_api: 'Z.AI',
  deepseek_api: 'DeepSeek',
  openai_api: 'OpenAI',
  gemini_api: 'Gemini',
  gcp: 'GCP',
};

function useHealthCountdown(lastCheckIso: string | undefined, intervalSeconds: number | undefined): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!lastCheckIso || !intervalSeconds) return null;
  const last = parseTimestamp(lastCheckIso).getTime();
  const next = last + intervalSeconds * 1000;
  return Math.max(0, Math.ceil((next - now) / 1000));
}

interface DeploySectionProps {
  deployState: DeployState | null;
  onPausePipeline: () => Promise<void>;
  onResumePipeline: () => Promise<void>;
  onResetHealth: () => Promise<void>;
  onRollback: () => Promise<void>;
  onMarkStable: () => Promise<void>;
  onRepairState: () => Promise<void>;
  // Standing failed-rollback signal. When set, the last manual rollback's Cloud
  // Run traffic shift failed (backend HTTP 200 status='rollback_failed') and
  // traffic still serves the bad revision. Rendered as a durable banner so the
  // failure stays visible after the transient error toast auto-dismisses.
  // Optional so existing callers/tests compile unchanged.
  failedRollback?: FailedRollbackInfo | null;
  zoneHealth: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null;
}

export function DeploySection({
  deployState,
  onPausePipeline,
  onResumePipeline,
  onResetHealth,
  onRollback,
  onMarkStable,
  onRepairState,
  failedRollback,
  zoneHealth,
}: DeploySectionProps) {
  const lastCheckIso = typeof deployState?.external_health?.last_check === 'string'
    ? (deployState.external_health.last_check as string)
    : undefined;
  const nextCheckSeconds = useHealthCountdown(lastCheckIso, deployState?.health_check_interval_seconds);
  return (
    <div className="space-y-4">
      {/* Deploy Health */}
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-300">Deploy Health</h3>
        {deployState ? (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-2 rounded-md border border-neutral-800 bg-workbench-layer p-3 sm:grid-cols-3">
              <div>
                <span className="mb-1 block text-ui-md uppercase tracking-wide text-neutral-500">Status</span>
                <span className={`text-sm font-semibold ${
                  deployState.status === 'STABLE' ? 'text-emerald-400' :
                  deployState.status === 'MONITORING' ? 'text-blue-400' :
                  deployState.status === 'PAUSED' ? 'text-amber-400' :
                  deployState.status === 'ROLLED_BACK' ? 'text-red-400' :
                  deployState.status === 'HALTED' ? 'text-red-500' : 'text-neutral-400'
                }`}>{deployState.status}</span>
              </div>
              <div>
                <span className="mb-1 block text-ui-md uppercase tracking-wide text-neutral-500">{deployState.rolled_back_from_sha ? 'Live SHA' : 'SHA'}</span>
                <span className="text-sm text-neutral-300 font-mono">{deployState.current_sha?.slice(0, 8) || '\u2014'}</span>
                {deployState.status === 'ROLLED_BACK' && deployState.rolled_back_from_sha && (
                  <span className="text-ui-md text-red-400/80 font-mono block mt-0.5" title={deployState.rolled_back_from_sha}>
                    rolled back from {deployState.rolled_back_from_sha.slice(0, 8)}
                  </span>
                )}
              </div>
              {(deployState.failure_rate ?? 0) > 0 && (
                <div>
                  <span className="mb-1 block text-ui-md uppercase tracking-wide text-neutral-500">Failure Rate</span>
                  <span className={`text-sm font-mono ${(deployState.failure_rate ?? 0) > 0.3 ? 'text-red-400' : 'text-neutral-300'}`}>
                    {((deployState.failure_rate ?? 0) * 100).toFixed(0)}%
                  </span>
                </div>
              )}
            </div>

            {deployState.monitoring_window !== null && (
              <div className="space-y-2 rounded-md border border-neutral-800 bg-workbench-layer p-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-neutral-500">Monitoring Progress</span>
                  <span className="text-xs text-blue-400 font-mono">{deployState.monitoring_window.percent}%</span>
                </div>
                <div className="h-1.5 w-full rounded-full bg-workbench-panel">
                  <div
                    className="h-1.5 rounded-full bg-blue-500 transition-all duration-500"
                    style={{ width: `${deployState.monitoring_window.percent}%` }}
                  />
                </div>
                <div className="flex items-center justify-between text-ui-md">
                  <span className="text-neutral-500">
                    {Math.floor(deployState.monitoring_window.elapsed_seconds / 60)}m / {Math.floor(deployState.monitoring_window.total_seconds / 60)}m elapsed
                  </span>
                  {deployState.task_results_count > 0 && (
                    <span className="text-neutral-400">{deployState.task_results_count} tasks monitored</span>
                  )}
                </div>
              </div>
            )}

            {deployState.rollback_reason && (
              <p className="rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{deployState.rollback_reason}</p>
            )}
            {deployState.pause_reason && (
              <p className="rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{deployState.pause_reason}</p>
            )}

            {/* External dependency health */}
            {deployState.external_health && (() => {
              const externalHealth = deployState.external_health;
              return (
              <div className="space-y-2 rounded-md border border-neutral-800 bg-workbench-layer p-3">
                <div className="mb-1 text-ui-md uppercase tracking-wide text-neutral-500">External Dependencies</div>
                {EXTERNAL_DEP_ORDER.filter((dep) => {
                  if (!(dep in externalHealth)) return false;
                  return typeof externalHealth[dep] === 'boolean';
                }).map((dep) => {
                  const healthy = deployState.external_health?.[dep];
                  const error = (deployState.external_health?.errors as Record<string, string> | undefined)?.[dep];
                  if (healthy === undefined) return null;
                  return (
                    <div key={dep} className="space-y-0.5">
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-neutral-400">{EXTERNAL_DEP_LABELS[dep] ?? dep.replace('_', ' ')}</span>
                        <span className={`text-xs font-semibold ${healthy ? 'text-emerald-400' : 'text-red-400'}`}>
                          {healthy ? 'OK' : 'DOWN'}
                        </span>
                      </div>
                      {error && (
                        <p className="break-all rounded bg-red-950/20 px-2 py-1 text-ui-md text-red-400/70">{String(error)}</p>
                      )}
                    </div>
                  );
                })}
                {typeof deployState.external_health?.last_check === 'string' && (
                  <div className="mt-1 flex items-center justify-between border-t border-neutral-800 pt-1">
                    <span className="text-ui-md text-neutral-600">Last check</span>
                    <span className="text-ui-md text-neutral-500 font-mono">
                      {parseTimestamp(deployState.external_health.last_check as string).toLocaleTimeString()}
                    </span>
                  </div>
                )}
                {nextCheckSeconds !== null && (
                  <div className="flex items-center justify-between">
                    <span className="text-ui-md text-neutral-600">Next check</span>
                    <span className="text-ui-md text-blue-400 font-mono">
                      {nextCheckSeconds === 0 ? 'running…' : `in ${nextCheckSeconds}s`}
                      <span className="text-neutral-600"> (every {deployState.health_check_interval_seconds ?? 60}s)</span>
                    </span>
                  </div>
                )}
                {(deployState.health_failure_streak ?? 0) > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-ui-md text-neutral-600">Failure streak</span>
                    <span className="text-ui-md text-amber-400 font-mono">
                      {deployState.health_failure_streak}/{deployState.health_failure_streak_threshold ?? 3} before pause
                    </span>
                  </div>
                )}
              </div>
              );
            })()}

            {deployState.status === 'PAUSED' && deployState.pre_pause_max_vms != null && (
              <p className="rounded-md border border-neutral-800 bg-workbench-layer px-3 py-2 text-xs text-neutral-400">
                Will restore to {deployState.pre_pause_max_vms} VM{deployState.pre_pause_max_vms !== 1 ? 's' : ''}
              </p>
            )}
            {deployState.status === 'PAUSED' && (deployState.health_recovery_streak ?? 0) > 0 && (
              <div className="space-y-2 rounded-md border border-neutral-800 bg-workbench-layer p-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-neutral-500">Recovery Progress</span>
                  <span className="text-xs text-emerald-400 font-mono">
                    {deployState.health_recovery_streak}/{deployState.health_recovery_streak_required ?? 3} healthy checks
                  </span>
                </div>
                {deployState.health_recovery_flap_escalated && (
                  <p className="text-ui-md text-orange-400 bg-orange-950/20 rounded px-2 py-1 flex items-start gap-1.5">
                    <Zap className="w-3 h-3 mt-0.5 shrink-0" />
                    <span>
                      Recovery bar raised to {deployState.health_recovery_streak_required ?? 3} because the
                      pipeline has been flapping (repeated auto-pause/resume); the full recent probe history must
                      pass before it auto-resumes.
                    </span>
                  </p>
                )}
                <div className="h-1.5 w-full rounded-full bg-workbench-panel">
                  <div
                    className="h-1.5 rounded-full bg-emerald-500 transition-all duration-500"
                    style={{ width: `${Math.min(100, (deployState.health_recovery_streak / (deployState.health_recovery_streak_required ?? 3)) * 100)}%` }}
                  />
                </div>
                {Array.isArray(deployState.health_recovery_history) && (
                  <div className="flex items-center justify-between pt-1">
                    <span className="text-ui-md text-neutral-600">Last {deployState.health_recovery_history_size ?? 5} probes</span>
                    <div className="flex gap-1">
                      {Array.from({ length: deployState.health_recovery_history_size ?? 5 }).map((_, i) => {
                        const hist = deployState.health_recovery_history ?? [];
                        const probe = hist[i];
                        const isEmpty = probe === undefined;
                        const color = isEmpty ? 'bg-workbench-layer' : probe ? 'bg-emerald-500' : 'bg-red-500';
                        return <span key={i} className={`w-2.5 h-2.5 rounded-full ${color}`} title={isEmpty ? 'no probe yet' : probe ? 'healthy' : 'failed'} />;
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Durable failed-rollback banner. The rollback error toast
                auto-dismisses after a few seconds, after which a STABLE surface
                is indistinguishable from a successful rollback (false success by
                absence). This standing banner keeps the failure visible until
                the operator resolves it (a successful rollback, mark-stable, or
                any deploy-state status change clears it upstream in the hook),
                and the adjacent Rollback button below invites the retry. */}
            {failedRollback && (
              <div className="flex items-start gap-2 rounded-md border border-red-900/50 bg-red-950/30 px-3 py-2.5" role="alert">
                <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
                <div className="space-y-0.5 min-w-0">
                  <p className="text-xs font-semibold text-red-400">Last rollback failed — traffic still serves the current revision</p>
                  <p className="text-ui-md text-red-400/80 break-all">
                    Traffic still serves{' '}
                    <span className="font-mono">{failedRollback.serving_sha?.slice(0, 8) ?? 'the current revision'}</span>
                    . Retry the rollback below.
                  </p>
                  {failedRollback.gcloud_error && (
                    <p className="text-ui-md text-red-400/70 font-mono break-all">{failedRollback.gcloud_error}</p>
                  )}
                </div>
              </div>
            )}

            {/* Action buttons */}
            <div className="flex gap-2 flex-wrap">
              {deployState.status === 'PAUSED' || deployState.status === 'ROLLED_BACK' ? (
                <button onClick={onResumePipeline}
                  className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-emerald-900/50 bg-emerald-950/30 px-3 py-2 text-xs text-emerald-300 transition-colors hover:bg-emerald-900/40">
                  <Play className="w-3.5 h-3.5" /> Resume
                </button>
              ) : deployState.status === 'STABLE' || deployState.status === 'MONITORING' ? (
                <button onClick={onPausePipeline}
                  className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-amber-900/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-300 transition-colors hover:bg-amber-900/40">
                  <Pause className="w-3.5 h-3.5" /> Pause
                </button>
              ) : null}
              {(deployState.status === 'MONITORING' || deployState.status === 'ROLLED_BACK' || deployState.status === 'HALTED') && (
                <button onClick={onMarkStable}
                  className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-hover px-3 py-2 text-xs text-emerald-300 transition-colors hover:bg-workbench-hover">
                  <CheckCircle className="w-3.5 h-3.5" /> Mark Stable
                </button>
              )}
              {deployState.status === 'PAUSED' && (
                <button
                  onClick={() => { if (window.confirm('Force-reset health and resume immediately? This bypasses the healthy-checks gate.')) onResetHealth(); }}
                  className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-orange-900/50 bg-orange-950/30 px-3 py-2 text-xs text-orange-300 transition-colors hover:bg-orange-900/40">
                  <Zap className="w-3.5 h-3.5" /> Force Reset
                </button>
              )}
              <button
                onClick={() => { if (window.confirm('Rollback to last known good revision? This will pause the pipeline.')) onRollback(); }}
                disabled={deployState.status === 'ROLLED_BACK' || !deployState.last_known_good_sha || deployState.last_known_good_sha === deployState.current_sha}
                className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-red-900/50 bg-red-950/30 px-3 py-2 text-xs text-red-300 transition-colors hover:bg-red-900/40 disabled:cursor-not-allowed disabled:opacity-30">
                <RotateCw className="w-3.5 h-3.5" /> Rollback
              </button>
              <button onClick={onRepairState}
                className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-hover px-3 py-2 text-xs text-neutral-300 transition-colors hover:bg-workbench-hover">
                <Wrench className="w-3.5 h-3.5" /> Repair
              </button>
            </div>
          </div>
        ) : (
          <p className="text-xs text-neutral-600">Loading...</p>
        )}
      </div>

      {/* Zone Health */}
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-300">Zone Health</h3>
        {zoneHealth ? (
          <div className="space-y-2 rounded-md border border-neutral-800 bg-workbench-layer p-3">
            {Object.entries(zoneHealth).map(([zone, data]) => {
              const region = zone.split('-').slice(0, 2).join('-');
              const dotColor = data.status === 'healthy' ? 'bg-emerald-500'
                : data.status === 'exhausted' ? 'bg-red-500 animate-pulse'
                : 'bg-amber-500';
              const labelColor = data.status === 'healthy' ? 'text-neutral-600'
                : data.status === 'exhausted' ? 'text-red-400'
                : 'text-amber-400';
              return (
                <div key={zone} className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <span className={`w-2 h-2 rounded-full ${dotColor}`} />
                    <span className="text-xs text-neutral-400 font-mono">{zone}</span>
                  </div>
                  <span className={`text-xs ${labelColor}`}>
                    {data.status === 'healthy' ? region
                      : `${data.consecutive_failures} fail${data.consecutive_failures !== 1 ? 's' : ''}${data.last_failure_reason ? ` (${data.last_failure_reason})` : ''}`}
                  </span>
                </div>
              );
            })}
            {Object.values(zoneHealth).every(d => d.status === 'healthy') && (
              <p className="text-xs text-emerald-500/60 mt-1">All zones healthy</p>
            )}
            {Object.values(zoneHealth).some(d => d.status === 'exhausted') && (
              <p className="text-xs text-amber-400/80 mt-1">
                {Object.values(zoneHealth).filter(d => d.status === 'healthy').length}/{Object.keys(zoneHealth).length} zones available
              </p>
            )}
          </div>
        ) : (
          <p className="text-xs text-neutral-600">No data yet</p>
        )}
      </div>

      {/* Release Gate Thresholds */}
      <MergeGradeGateSettings />
    </div>
  );
}
