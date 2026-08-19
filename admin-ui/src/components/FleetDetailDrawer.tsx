import { X } from 'lucide-react';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import type { Project } from '../types/project';
import { resolveAccess } from '../accessControl';

interface FleetDetailDrawerProps {
  onClose: () => void;
  onViewTask: (taskId: string) => void;
  projects: Project[];
  selectedProjectId?: string | null;
  fleetRunnersRefreshSignal?: number;
  live?: boolean;
}

export function FleetDetailDrawer({ onClose, onViewTask, projects, selectedProjectId, fleetRunnersRefreshSignal, live }: FleetDetailDrawerProps) {
  const projectRole = projects.find(project => project.id === selectedProjectId)?.my_role;
  const canManageProject = resolveAccess(undefined, projectRole).canManageProject;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-2xl h-full bg-workbench-shell border-l border-neutral-800 shadow-2xl flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800 shrink-0">
          <h2 className="text-sm font-semibold text-neutral-200">Self-hosted runners</h2>
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            onClick={onClose}
            className="text-neutral-400 hover:text-neutral-200 transition-colors p-1"
            title="Close fleet details"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-auto custom-scrollbar">
          {selectedProjectId ? (
            <FleetRunnersPanel
              projectId={selectedProjectId}
              refreshSignal={fleetRunnersRefreshSignal}
              live={live}
              onNavigateToTask={onViewTask}
              readOnly={!canManageProject}
            />
          ) : (
            <div className="p-6 text-xs text-neutral-500 text-center">
              Select a project to view self-hosted runners.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
