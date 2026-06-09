import { useState } from 'react';
import { ChevronDown, ChevronUp, Stethoscope, AlertTriangle, Clock, Zap, Ban, RotateCcw, X } from 'lucide-react';
import type { ProjectDiagnostics, StuckTask, FailingTaskTypes } from '../types/diagnostics';
import { formatDurationSec } from '../utils/time';
import { API_BASE_URL, authGet } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';

const STUCK_REASON_LABELS: Record<StuckTask['stuck_reason'], string> = {
  provision_timeout: 'VM provision timed out',
  no_heartbeat: 'Agent heartbeat lost',
  long_queued: 'Queued too long (30m+)',
  stuck_deps: 'Dependencies failed',
  evaluation_timeout: 'Evaluation timed out (2h+)',
};

interface DiagnosticsPanelProps {
  diagnostics: ProjectDiagnostics | null;
  onViewTask: (taskId: string) => void;
  handleRetry: (taskId: string) => void;
  handleCancel: (taskId: string) => void;
  setConfirmDialog: (dialog: ConfirmDialogConfig | null) => void;
}

interface ConfirmDialogConfig {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
}

const RETRYABLE_STATUSES = new Set(['FAILED']);
const CANCELLABLE_STATUSES = new Set(['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'BLOCKED', 'PARKED']);

export function DiagnosticsPanel({ diagnostics, onViewTask, handleRetry, handleCancel, setConfirmDialog }: DiagnosticsPanelProps) {
  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem('blueprint-admin:diagnosticsPanelCollapsed') === 'true';
  });
  const [failingTypes, setFailingTypes] = useState<FailingTaskTypes | null>(null);

  usePoller(async (signal) => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/failing-task-types`);
      if (!signal.aborted && res.ok) setFailingTypes(await res.json());
    } catch { /* leave previous value */ }
  }, { intervalMs: DEFAULT_POLL_MS });

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:diagnosticsPanelCollapsed', String(next));
  };

  const stuckCount = diagnostics?.stuck_tasks.length ?? 0;
  const cbState = diagnostics?.circuit_breaker.state ?? 'CLOSED';
  const cbFailures = diagnostics?.circuit_breaker.failure_count ?? 0;
  const zonesAvailable = diagnostics?.zone_health.available ?? 0;
  const exhaustedZones = diagnostics?.zone_health.exhausted ?? [];

  const showRetry = (task: StuckTask) => RETRYABLE_STATUSES.has(task.status);
  const showCancel = (task: StuckTask) => CANCELLABLE_STATUSES.has(task.status);

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors min-h-[44px] px-1"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Stethoscope className="w-3 h-3" />
          Pipeline Health
          {stuckCount > 0 && (
            <span className="ml-1 bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
              {stuckCount} stuck
            </span>
          )}
          {failingTypes && failingTypes.gated_types.length > 0 && (
            <span className="ml-1 bg-amber-500/20 border border-amber-500/40 text-amber-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
              {failingTypes.gated_types.length} type{failingTypes.gated_types.length !== 1 ? 's' : ''} gated
            </span>
          )}
          {collapsed && diagnostics && (
            <span className="text-[10px] text-neutral-500 font-mono ml-1 normal-case tracking-normal font-normal">
              {diagnostics.in_progress_count} active · {diagnostics.queued_count}Q
              {cbState !== 'CLOSED' && (
                <span className={cbState === 'OPEN' ? 'text-red-400' : 'text-amber-400'}> · CB {cbState}</span>
              )}
            </span>
          )}
        </button>
      </div>

      {!collapsed && diagnostics && (
        <div className="mt-2 bg-neutral-900 border border-neutral-800 rounded-lg p-3 text-xs space-y-3">
          {/* Stuck Tasks */}
          <div>
            <div className="flex items-center gap-1.5 mb-1.5">
              <AlertTriangle className={`w-3 h-3 ${stuckCount > 0 ? 'text-red-400' : 'text-neutral-600'}`} />
              <span className={`font-medium ${stuckCount > 0 ? 'text-red-400' : 'text-neutral-500'}`}>
                {stuckCount > 0 ? `${stuckCount} stuck task${stuckCount !== 1 ? 's' : ''}` : 'No stuck tasks'}
              </span>
            </div>
            {stuckCount > 0 && (
              <div className="space-y-2 ml-4">
                {diagnostics.stuck_tasks.map((task) => (
                  <div key={task.task_id}>
                    <div className="flex items-start gap-2 flex-wrap">
                      <button
                        onClick={() => onViewTask(task.task_id)}
                        className="text-neutral-300 hover:text-blue-400 transition-colors truncate max-w-[200px] text-left min-h-[44px]"
                        title={task.title}
                      >
                        {task.title}
                      </button>
                      <span className="text-neutral-500 shrink-0">—</span>
                      <span className="text-amber-400 shrink-0">{STUCK_REASON_LABELS[task.stuck_reason]}</span>
                      <span className="text-neutral-600 shrink-0 font-mono">
                        <Clock className="w-3 h-3 inline mr-0.5" />{formatDurationSec(task.stuck_for_seconds)}
                      </span>
                      {task.attempt_count > 1 && (
                        <span className="text-neutral-600 shrink-0">×{task.attempt_count}</span>
                      )}
                      {task.stuck_reason === 'stuck_deps' && task.unresolvable_deps?.length > 0 && (
                        <span className="text-neutral-600 font-mono">
                          deps: {task.unresolvable_deps.map(d => d.slice(0, 8)).join(', ')}
                        </span>
                      )}
                      {(showRetry(task) || showCancel(task)) && (
                        <div className="flex items-center gap-1.5 ml-auto">
                          {showRetry(task) && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                handleRetry(task.task_id);
                              }}
                              className="inline-flex items-center gap-1 rounded border border-blue-500/20 bg-blue-500/10 px-2 py-1 min-h-[44px] md:min-h-auto text-[9px] font-semibold text-blue-400 hover:bg-blue-500/20 transition-colors"
                              title={`Retry ${task.title}`}
                            >
                              <RotateCcw className="w-2.5 h-2.5" />
                              Retry
                            </button>
                          )}
                          {showCancel(task) && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setConfirmDialog({
                                  title: 'Cancel task?',
                                  message: `This will cancel "${task.title}".`,
                                  confirmLabel: 'Cancel task',
                                  onConfirm: () => {
                                    handleCancel(task.task_id);
                                    setConfirmDialog(null);
                                  },
                                });
                              }}
                              className="inline-flex items-center gap-1 rounded border border-red-500/20 bg-red-500/10 px-2 py-1 min-h-[44px] md:min-h-auto text-[9px] font-semibold text-red-400 hover:bg-red-500/20 transition-colors"
                              title={`Cancel ${task.title}`}
                            >
                              <X className="w-2.5 h-2.5" />
                              Cancel
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                    {task.remediation_hint && (
                      <div className="mt-0.5 ml-0 text-[10px] text-neutral-500 italic">
                        {task.remediation_hint}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Per-type failure gate */}
          {failingTypes && failingTypes.gated_types.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 mb-1.5">
                <Ban className="w-3 h-3 text-amber-400" />
                <span className="font-medium text-amber-400">
                  {failingTypes.gated_types.length} task type{failingTypes.gated_types.length !== 1 ? 's' : ''} gated
                </span>
                {failingTypes.anomaly_tightened && (
                  <span className="text-[10px] text-amber-300 font-mono px-1 py-0.5 rounded bg-amber-500/10 border border-amber-500/30">
                    anomaly-tightened gate ({Math.round(failingTypes.global_failure_threshold * 100)}%)
                  </span>
                )}
              </div>
              <div className="space-y-1 ml-4">
                {failingTypes.gated_types.map((g) => (
                  <div key={g.task_type} className="flex flex-wrap items-center gap-2">
                    <span className="text-neutral-300 font-mono">{g.task_type}</span>
                    <span className="text-neutral-500">—</span>
                    <span className="text-amber-400">
                      gated at {Math.round(g.failure_rate * 100)}% failure rate
                    </span>
                    <span className="text-neutral-600 font-mono">n={g.sample_size}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Zone Health */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={`w-1.5 h-1.5 rounded-full ${exhaustedZones.length === 0 ? 'bg-emerald-500' : 'bg-red-500'}`} />
            <span className="text-neutral-400">
              Zones: <span className={`font-medium ${exhaustedZones.length === 0 ? 'text-emerald-400' : 'text-neutral-300'}`}>{zonesAvailable} available</span>
            </span>
            {exhaustedZones.length > 0 && (
              <span className="text-red-400 font-mono ml-1">
                exhausted: {exhaustedZones.join(', ')}
              </span>
            )}
          </div>

          {/* Circuit Breaker */}
          <div className="flex items-center gap-1.5">
            <Zap className="w-3 h-3 text-neutral-500" />
            <span className="text-neutral-400">Circuit breaker:</span>
            <span className={`font-medium px-1.5 py-0.5 rounded text-[10px] ${
              cbState === 'CLOSED' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' :
              cbState === 'OPEN' ? 'bg-red-500/10 text-red-400 border border-red-500/30' :
              'bg-amber-500/10 text-amber-400 border border-amber-500/30'
            }`}>
              {cbState}
            </span>
            {cbFailures > 0 && (
              <span className="text-neutral-500 font-mono">{cbFailures} failures</span>
            )}
          </div>
        </div>
      )}

      {!collapsed && !diagnostics && (
        <div className="mt-2 bg-neutral-900 border border-neutral-800 rounded-lg p-3 text-xs text-neutral-500">
          Select a project to view diagnostics
        </div>
      )}
    </div>
  );
}
