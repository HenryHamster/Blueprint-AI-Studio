import { AlertTriangle, X, ExternalLink } from 'lucide-react';

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

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onViewTask?: (taskId: string) => void;
}

export function AlertsBanner({ alerts, onDismiss, onViewTask }: AlertsBannerProps) {
  if (alerts.length === 0) return null;

  return (
    <div className="mb-4 flex flex-col gap-2 shrink-0">
      {alerts.map(alert => (
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
    </div>
  );
}
