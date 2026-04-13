import { useState } from 'react';
import { ChevronDown, ChevronUp, Stethoscope, AlertTriangle, Clock, Zap } from 'lucide-react';
import type { ProjectDiagnostics, StuckTask } from '../types/diagnostics';
import { formatDurationSec } from '../utils/time';

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
}

export function DiagnosticsPanel({ diagnostics, onViewTask }: DiagnosticsPanelProps) {
  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem('blueprint-admin:diagnosticsPanelCollapsed') === 'true';
  });

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

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Stethoscope className="w-3 h-3" />
          Pipeline Health
          {stuckCount > 0 && (
            <span className="ml-1 bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none">
              {stuckCount} stuck
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
              <div className="space-y-1.5 ml-4">
                {diagnostics.stuck_tasks.map((task) => (
                  <div key={task.task_id} className="flex items-start gap-2">
                    <button
                      onClick={() => onViewTask(task.task_id)}
                      className="text-neutral-300 hover:text-blue-400 transition-colors truncate max-w-[200px] text-left"
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
                      <span className="text-neutral-600 font-mono shrink-0">
                        deps: {task.unresolvable_deps.map(d => d.slice(0, 8)).join(', ')}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Zone Health */}
          <div className="flex items-center gap-1.5">
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
