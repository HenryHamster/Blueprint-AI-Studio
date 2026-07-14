import { useState, useCallback } from 'react';
import { AlertTriangle, Check, X, ExternalLink, ChevronDown, ChevronUp, Loader2 } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';

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

/**
 * Minimal recurrence hint for an alert. We intentionally surface only the
 * dedup context an operator can act on — how many times it fired and when it
 * was last seen — and drop the internal taxonomy chips (domain / event_type /
 * source) that carried no operator-facing action.
 */
function AlertMetadata({ alert }: { alert: Alert }) {
  if (alert.occurrence_count && alert.occurrence_count > 1) {
    return (
      <div className="mt-1 text-ui-sm text-neutral-500">
        {alert.occurrence_count} occurrences
        {alert.last_seen_at ? ` · last seen ${timeAgo(alert.last_seen_at)}` : ''}
      </div>
    );
  }
  if (alert.last_seen_at) {
    return <div className="mt-1 text-ui-sm text-neutral-500">Last seen {timeAgo(alert.last_seen_at)}</div>;
  }
  return null;
}

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
  projectId?: string;
}

export function AlertsBanner({ alerts, onDismiss, onDismissAll, onViewTask, projectId }: AlertsBannerProps) {
  const [expanded, setExpanded] = useState(false);
  const [acknowledgingAlertId, setAcknowledgingAlertId] = useState<string | null>(null);
  const [acknowledgedAlertIds, setAcknowledgedAlertIds] = useState<Set<string>>(new Set());

  const handleAcknowledge = useCallback(async (alert: Alert) => {
    if (!projectId || acknowledgingAlertId === alert.id) return;
    if (alert.acknowledged_at || acknowledgedAlertIds.has(alert.id)) return;
    setAcknowledgingAlertId(alert.id);
    try {
      const res = await apiFetch(`/api/admin/alerts/${alert.id}/acknowledge`, {
        method: 'POST',
      });
      if (res.ok) {
        setAcknowledgedAlertIds(prev => new Set(prev).add(alert.id));
      } else {
        console.error(`Failed to acknowledge alert ${alert.id}: HTTP ${res.status}`);
      }
    } catch (err) {
      console.error(`Failed to acknowledge alert ${alert.id}:`, err);
    } finally {
      setAcknowledgingAlertId(null);
    }
  }, [projectId, acknowledgingAlertId, acknowledgedAlertIds]);

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
              className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0"
            >
              {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              <span className="hidden md:inline">{expanded ? 'Collapse' : `Show all ${alerts.length} alerts`}</span>
            </button>
          )}
          <button
            onClick={onDismissAll}
            className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center px-2 md:px-0"
          >
            <span className="hidden md:inline">Dismiss all</span>
            <span className="md:hidden">✕</span>
          </button>
        </div>
      )}
      {visibleAlerts.map(alert => (
        <div key={alert.id} className="flex flex-col gap-1">
          <div className={`${severityStyles[alert.severity]} border rounded-lg px-4 py-3 text-sm flex items-center gap-3`}>
            <AlertTriangle className={`w-4 h-4 shrink-0 ${iconColors[alert.severity]}`} />
            <div className="flex-1 min-w-0">
              <span className="font-medium">{alert.title}</span>
              <span className="text-neutral-500 mx-2">&middot;</span>
              <span className="opacity-80">{alert.message}</span>
              <AlertMetadata alert={alert} />
            </div>
            {alert.task_id && onViewTask && (() => {
              const taskId = alert.task_id;
              return (
                <button
                  onClick={() => onViewTask(taskId)}
                  className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto"
                  title="View task"
                >
                  <ExternalLink className="w-3.5 h-3.5" />
                </button>
              );
            })()}
            {projectId && (() => {
              const isAcknowledged = !!(alert.acknowledged_at) || acknowledgedAlertIds.has(alert.id);
              const isAcknowledging = acknowledgingAlertId === alert.id;
              if (isAcknowledged) {
                return (
                  <span
                    className="shrink-0 opacity-40 text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0"
                    title="Acknowledged"
                  >
                    <Check className="w-3.5 h-3.5" />
                    <span className="hidden md:inline">Acknowledged</span>
                  </span>
                );
              }
              return (
                <button
                  type="button"
                  onClick={() => void handleAcknowledge(alert)}
                  disabled={isAcknowledging}
                  className="shrink-0 opacity-60 hover:opacity-100 disabled:opacity-30 transition-opacity text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0"
                  title="Acknowledge alert to suppress further re-pages"
                  aria-label="Acknowledge alert"
                >
                  {isAcknowledging ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                  <span className="hidden md:inline">Acknowledge</span>
                </button>
              );
            })()}
            <button
              onClick={() => onDismiss(alert.id)}
              className="shrink-0 opacity-60 hover:opacity-100 transition-opacity p-2.5 md:p-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              title="Dismiss"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      ))}
      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors flex items-center justify-center min-h-[44px] sm:min-h-0"
        >
          +{hiddenCount} more alert{hiddenCount > 1 ? 's' : ''}
        </button>
      )}
    </div>
  );
}
