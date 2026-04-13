import { useState } from 'react';
import { AlertTriangle, X, ExternalLink, ChevronDown, ChevronUp } from 'lucide-react';

export type { Alert } from '../types/alert';
import type { Alert } from '../types/alert';

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

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
}

export function AlertsBanner({ alerts, onDismiss, onDismissAll, onViewTask }: AlertsBannerProps) {
  const [expanded, setExpanded] = useState(false);
  if (alerts.length === 0) return null;

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
          {alert.task_id && onViewTask && (
            <button
              onClick={() => onViewTask(alert.task_id!)}
              className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center gap-1"
              title="View task"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </button>
          )}
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
    </div>
  );
}
