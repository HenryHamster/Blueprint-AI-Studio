import { useEffect, useState } from 'react';
import { Play, Pause, RotateCw, Wrench, CheckCircle, Zap } from 'lucide-react';
import { parseTimestamp } from '../utils/time';
import type { DeployState } from '../types/settings';

const EXTERNAL_DEP_ORDER = ['github_api', 'db', 'dispatch_api', 'anthropic_api', 'openai_api', 'gemini_api', 'gcp'] as const;

const EXTERNAL_DEP_LABELS: Record<string, string> = {
  github_api: 'GitHub',
  db: 'DB',
  dispatch_api: 'Dispatch',
  anthropic_api: 'Anthropic',
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
  zoneHealth,
}: DeploySectionProps) {
  const lastCheckIso = typeof deployState?.external_health?.last_check === 'string'
    ? (deployState.external_health.last_check as string)
    : undefined;
  const nextCheckSeconds = useHealthCountdown(lastCheckIso, deployState?.health_check_interval_seconds);
  return (
    <div className="space-y-6">
      {/* Deploy Health */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Deploy Health</h3>
        {deployState ? (
          <div className="space-y-3">
            <div className="grid grid-cols-3 gap-4 bg-neutral-900 rounded-lg border border-neutral-800 p-4">
              <div>
                <span className="text-[11px] text-neutral-500 block mb-1">Status</span>
                <span className={`text-sm font-semibold ${
                  deployState.status === 'STABLE' ? 'text-emerald-400' :
                  deployState.status === 'MONITORING' ? 'text-blue-400' :
                  deployState.status === 'PAUSED' ? 'text-amber-400' :
                  deployState.status === 'ROLLED_BACK' ? 'text-red-400' :
                  deployState.status === 'HALTED' ? 'text-red-500' : 'text-neutral-400'
                }`}>{deployState.status}</span>
              </div>
              <div>
                <span className="text-[11px] text-neutral-500 block mb-1">{deployState.rolled_back_from_sha ? 'Live SHA' : 'SHA'}</span>
                <span className="text-sm text-neutral-300 font-mono">{deployState.current_sha?.slice(0, 8) || '\u2014'}</span>
                {deployState.status === 'ROLLED_BACK' && deployState.rolled_back_from_sha && (
                  <span className="text-[11px] text-red-400/80 font-mono block mt-0.5" title={deployState.rolled_back_from_sha}>
                    rolled back from {deployState.rolled_back_from_sha.slice(0, 8)}
                  </span>
                )}
              </div>
              {(deployState.failure_rate ?? 0) > 0 && (
                <div>
                  <span className="text-[11px] text-neutral-500 block mb-1">Failure Rate</span>
                  <span className={`text-sm font-mono ${(deployState.failure_rate ?? 0) > 0.3 ? 'text-red-400' : 'text-neutral-300'}`}>
                    {((deployState.failure_rate ?? 0) * 100).toFixed(0)}%
                  </span>
                </div>
              )}
            </div>

            {deployState.monitoring_window !== null && (
              <div className="bg-neutral-900 rounded-lg border border-neutral-800 p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-neutral-500">Monitoring Progress</span>
                  <span className="text-xs text-blue-400 font-mono">{deployState.monitoring_window.percent}%</span>
                </div>
                <div className="w-full bg-neutral-800 rounded-full h-2">
                  <div
                    className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                    style={{ width: `${deployState.monitoring_window.percent}%` }}
                  />
                </div>
                <div className="flex items-center justify-between text-[11px]">
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
              <p className="text-xs text-amber-400/80 bg-amber-950/20 rounded-lg px-3 py-2">{deployState.rollback_reason}</p>
            )}
            {deployState.pause_reason && (
              <p className="text-xs text-amber-400/80 bg-amber-950/20 rounded-lg px-3 py-2">{deployState.pause_reason}</p>
            )}

            {/* External dependency health */}
            {deployState.external_health && (() => {
              const externalHealth = deployState.external_health;
              return (
              <div className="bg-neutral-900 rounded-lg border border-neutral-800 p-4 space-y-2">
                <div className="text-[11px] text-neutral-500 uppercase tracking-wider mb-1">External Dependencies</div>
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
                        <p className="text-[11px] text-red-400/70 bg-red-950/20 rounded px-2 py-1 break-all">{String(error)}</p>
                      )}
                    </div>
                  );
                })}
                {typeof deployState.external_health?.last_check === 'string' && (
                  <div className="flex items-center justify-between mt-1 pt-1 border-t border-neutral-800">
                    <span className="text-[11px] text-neutral-600">Last check</span>
                    <span className="text-[11px] text-neutral-500 font-mono">
                      {parseTimestamp(deployState.external_health.last_check as string).toLocaleTimeString()}
                    </span>
                  </div>
                )}
                {nextCheckSeconds !== null && (
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-neutral-600">Next check</span>
                    <span className="text-[11px] text-blue-400 font-mono">
                      {nextCheckSeconds === 0 ? 'running…' : `in ${nextCheckSeconds}s`}
                      <span className="text-neutral-600"> (every {deployState.health_check_interval_seconds ?? 60}s)</span>
                    </span>
                  </div>
                )}
                {(deployState.health_failure_streak ?? 0) > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-neutral-600">Failure streak</span>
                    <span className="text-[11px] text-amber-400 font-mono">
                      {deployState.health_failure_streak}/{deployState.health_failure_streak_threshold ?? 3} before pause
                    </span>
                  </div>
                )}
              </div>
              );
            })()}

            {deployState.status === 'PAUSED' && deployState.pre_pause_max_vms != null && (
              <p className="text-xs text-neutral-400 bg-neutral-800/40 rounded-lg px-3 py-2">
                Will restore to {deployState.pre_pause_max_vms} VM{deployState.pre_pause_max_vms !== 1 ? 's' : ''}
              </p>
            )}
            {deployState.status === 'PAUSED' && (deployState.health_recovery_streak ?? 0) > 0 && (
              <div className="bg-neutral-900 rounded-lg border border-neutral-800 p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-neutral-500">Recovery Progress</span>
                  <span className="text-xs text-emerald-400 font-mono">
                    {deployState.health_recovery_streak}/{deployState.health_recovery_streak_required ?? 3} healthy checks
                  </span>
                </div>
                {deployState.health_recovery_flap_escalated && (
                  <p className="text-[11px] text-orange-400 bg-orange-950/20 rounded px-2 py-1 flex items-start gap-1.5">
                    <Zap className="w-3 h-3 mt-0.5 shrink-0" />
                    <span>
                      Recovery bar raised to {deployState.health_recovery_streak_required ?? 3} because the
                      pipeline has been flapping (repeated auto-pause/resume); the full recent probe history must
                      pass before it auto-resumes.
                    </span>
                  </p>
                )}
                <div className="w-full bg-neutral-800 rounded-full h-2">
                  <div
                    className="bg-emerald-500 h-2 rounded-full transition-all duration-500"
                    style={{ width: `${Math.min(100, (deployState.health_recovery_streak / (deployState.health_recovery_streak_required ?? 3)) * 100)}%` }}
                  />
                </div>
                {Array.isArray(deployState.health_recovery_history) && (
                  <div className="flex items-center justify-between pt-1">
                    <span className="text-[11px] text-neutral-600">Last {deployState.health_recovery_history_size ?? 5} probes</span>
                    <div className="flex gap-1">
                      {Array.from({ length: deployState.health_recovery_history_size ?? 5 }).map((_, i) => {
                        const hist = deployState.health_recovery_history ?? [];
                        const probe = hist[i];
                        const isEmpty = probe === undefined;
                        const color = isEmpty ? 'bg-neutral-700' : probe ? 'bg-emerald-500' : 'bg-red-500';
                        return <span key={i} className={`w-2.5 h-2.5 rounded-full ${color}`} title={isEmpty ? 'no probe yet' : probe ? 'healthy' : 'failed'} />;
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Action buttons */}
            <div className="flex gap-2 flex-wrap">
              {deployState.status === 'PAUSED' || deployState.status === 'ROLLED_BACK' ? (
                <button onClick={onResumePipeline}
                  className="bg-emerald-950/30 hover:bg-emerald-900/40 text-emerald-400 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-emerald-900/50 flex items-center gap-1.5">
                  <Play className="w-3.5 h-3.5" /> Resume
                </button>
              ) : deployState.status === 'STABLE' || deployState.status === 'MONITORING' ? (
                <button onClick={onPausePipeline}
                  className="bg-amber-950/30 hover:bg-amber-900/40 text-amber-400 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-amber-900/50 flex items-center gap-1.5">
                  <Pause className="w-3.5 h-3.5" /> Pause
                </button>
              ) : null}
              {(deployState.status === 'MONITORING' || deployState.status === 'ROLLED_BACK' || deployState.status === 'HALTED') && (
                <button onClick={onMarkStable}
                  className="bg-neutral-800 hover:bg-neutral-700 text-emerald-400 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-neutral-700 flex items-center gap-1.5">
                  <CheckCircle className="w-3.5 h-3.5" /> Mark Stable
                </button>
              )}
              {deployState.status === 'PAUSED' && (
                <button
                  onClick={() => { if (window.confirm('Force-reset health and resume immediately? This bypasses the healthy-checks gate.')) onResetHealth(); }}
                  className="bg-orange-950/30 hover:bg-orange-900/40 text-orange-400 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-orange-900/50 flex items-center gap-1.5">
                  <Zap className="w-3.5 h-3.5" /> Force Reset
                </button>
              )}
              <button
                onClick={() => { if (window.confirm('Rollback to last known good revision? This will pause the pipeline.')) onRollback(); }}
                disabled={deployState.status === 'ROLLED_BACK' || !deployState.last_known_good_sha || deployState.last_known_good_sha === deployState.current_sha}
                className="bg-red-950/30 hover:bg-red-900/40 disabled:opacity-30 disabled:cursor-not-allowed text-red-400 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-red-900/50 flex items-center gap-1.5">
                <RotateCw className="w-3.5 h-3.5" /> Rollback
              </button>
              <button onClick={onRepairState}
                className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-neutral-700 flex items-center gap-1.5">
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
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Zone Health</h3>
        {zoneHealth ? (
          <div className="bg-neutral-900 rounded-lg border border-neutral-800 p-4 space-y-2">
            {Object.entries(zoneHealth).map(([zone, data]) => {
              const region = zone.split('-').slice(0, 2).join('-');
              const dotColor = data.status === 'healthy' ? 'bg-emerald-500'
                : data.status === 'exhausted' ? 'bg-red-500 animate-pulse'
                : 'bg-amber-500';
              const labelColor = data.status === 'healthy' ? 'text-neutral-600'
                : data.status === 'exhausted' ? 'text-red-400'
                : 'text-amber-400';
              return (
                <div key={zone} className="flex items-center justify-between">
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
    </div>
  );
}
