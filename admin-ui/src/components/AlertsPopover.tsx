import { X } from 'lucide-react';
import { AlertsBanner } from './AlertsBanner';
import type { Alert } from '../types/alert';
import type { SrInvestigationSignal } from './SrTrendBreakdown';

interface AlertsPopoverProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
  projectId?: string;
  onSpawnSrInvestigation?: (signal: SrInvestigationSignal) => Promise<string | null>;
  activeInvestigationTaskIds?: Record<string, string> | null;
  onClose: () => void;
}

export function AlertsPopover({
  alerts,
  onDismiss,
  onDismissAll,
  onViewTask,
  projectId,
  onSpawnSrInvestigation,
  activeInvestigationTaskIds,
  onClose,
}: AlertsPopoverProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-end p-3 pt-16"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-neutral-900 border border-neutral-700 rounded-xl shadow-2xl w-full max-w-md max-h-[80vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800 shrink-0">
          <h2 className="text-sm font-semibold text-neutral-200">Alerts</h2>
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            onClick={onClose}
            className="text-neutral-400 hover:text-neutral-200 transition-colors p-1"
            title="Close alerts"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4 overflow-y-auto custom-scrollbar">
          <AlertsBanner
            alerts={alerts}
            onDismiss={(id) => {
              onDismiss(id);
              if (alerts.length <= 1) onClose();
            }}
            onDismissAll={() => {
              onDismissAll?.();
              onClose();
            }}
            onViewTask={(taskId) => {
              onViewTask?.(taskId);
              onClose();
            }}
            projectId={projectId}
            onSpawnSrInvestigation={onSpawnSrInvestigation}
            activeInvestigationTaskIds={activeInvestigationTaskIds}
          />
        </div>
      </div>
    </div>
  );
}
