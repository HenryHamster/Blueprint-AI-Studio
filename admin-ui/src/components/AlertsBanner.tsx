import { useState } from 'react';
import { AlertTriangle, X, ExternalLink, ChevronDown, ChevronUp, Activity } from 'lucide-react';

export type { Alert } from '../types/alert';
import type { Alert } from '../types/alert';
import type { PipelineRegression } from './DashboardHome';

export type { PipelineRegression };

const severityStyles: Record<Alert['severity'], string> = {
  ERROR: 'bg-red-950/50 border-red-900/50 text-red-400',
  WARNING: 'bg-amber-950/50 border-amber-900/50 text-amber-400',
  INFO: 'bg-blue-950/50 border-blue-900/50 text-blue-400',
};

const iconColors: Record<Alert['severity'], string> = {
  ERROR: 'text-red-500',
  WARNING: 'text-amber-500',
  INFO: 'text-blue-500',
};

/** Max alerts shown before collapsing into a summary bar. */
const COLLAPSE_THRESHOLD = 2;

const _REGRESSION_STATUS_PILL: Record<string, string> = {
  IN_PROGRESS: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  EVALUATING: 'bg-purple-500/15 text-purple-400 border-purple-500/30',
  COMPLETED: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  APPROVED: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  FAILED: 'bg-red-500/15 text-red-400 border-red-500/30',
};

function regressionStatusLabel(status: string | null | undefined): string {
  if (!status) return 'investigating';
  const s = status.toUpperCase();
  if (s === 'IN_PROGRESS') return 'investigating';
  if (s === 'EVALUATING') return 'evaluating';
  if (s === 'COMPLETED') return 'resolved';
  if (s === 'APPROVED') return 'approved';
  if (s === 'FAILED') return 'failed';
  return status.toLowerCase();
}

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
  regressions?: PipelineRegression[];
}

export function AlertsBanner({ alerts, onDismiss, onDismissAll, onViewTask, regressions }: AlertsBannerProps) {
  const [expanded, setExpanded] = useState(false);
  const activeRegressions = regressions?.filter(r => !r.resolved) ?? [];
  if (alerts.length === 0 && activeRegressions.length === 0) return null;

  const shouldCollapse = alerts.length > COLLAPSE_THRESHOLD;

  const visibleAlerts = shouldCollapse && !expanded ? alerts.slice(0, 1) : alerts;
  const hiddenCount = alerts.length - visibleAlerts.length;

  return (
    <div className="mb-4 flex flex-col gap-2 shrink-0">
      {alerts.length > 1 && onDismissAll && (
        <div className="flex justify-end gap-3">
          {shouldCollapse && (
            <button
              onClick={() => setExpanded(e => !e)}
              className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors flex items-center gap-1"
            >
              {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              {expanded ? 'Collapse' : `Show all ${alerts.length} alerts`}
            </button>
          )}
          <button
            onClick={onDismissAll}
            className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors"
          >
            Dismiss all
          </button>
        </div>
      )}
      {visibleAlerts.map(alert => (
        <div
          key={alert.id}
          className={`${severityStyles[alert.severity]} border rounded-lg px-4 py-3 text-sm flex items-center gap-3`}
        >
          <AlertTriangle className={`w-4 h-4 shrink-0 ${iconColors[alert.severity]}`} />
          <div className="flex-1 min-w-0">
            <span className="font-medium">{alert.title}</span>
            <span className="text-neutral-500 mx-2">&middot;</span>
            <span className="opacity-80">{alert.message}</span>
          </div>
          {alert.task_id && onViewTask && (() => {
            const taskId = alert.task_id;
            return (
            <button
              onClick={() => onViewTask(taskId)}
              className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center gap-1"
              title="View task"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </button>
            );
          })()}
          <button
            onClick={() => onDismiss(alert.id)}
            className="shrink-0 opacity-60 hover:opacity-100 transition-opacity"
            title="Dismiss"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      ))}
      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors text-center py-1"
        >
          +{hiddenCount} more alert{hiddenCount > 1 ? 's' : ''}
        </button>
      )}
      {activeRegressions.map(r => {
        const pillStyle = r.spawned_task_status
          ? (_REGRESSION_STATUS_PILL[r.spawned_task_status.toUpperCase()] ?? _REGRESSION_STATUS_PILL['IN_PROGRESS'])
          : 'bg-neutral-800/50 text-neutral-400 border-neutral-700/50';
        const taskId = r.spawned_task_id;
        return (
          <div
            key={`regression-${r.signal_name}`}
            className="bg-orange-950/30 border border-orange-900/40 rounded-lg px-4 py-2 text-sm flex items-center gap-3"
          >
            <Activity className="w-4 h-4 shrink-0 text-orange-500" />
            <div className="flex-1 min-w-0 flex items-center gap-2 flex-wrap">
              <span className="font-medium text-orange-300 capitalize">{r.signal_name.replace(/_/g, ' ')}</span>
              {r.regression_class && (
                <span className="text-[10px] text-neutral-500 font-mono">{r.regression_class}</span>
              )}
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${taskId ? pillStyle : 'bg-neutral-800/50 text-neutral-500 border-neutral-700/50'}`}
              >
                {taskId ? regressionStatusLabel(r.spawned_task_status) : 'no action'}
              </span>
            </div>
            {taskId && onViewTask && (
              <button
                onClick={() => onViewTask(taskId)}
                className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center gap-1"
                title={r.spawned_task_title ?? 'View investigation task'}
              >
                <ExternalLink className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
