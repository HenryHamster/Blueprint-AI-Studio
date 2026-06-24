import { X } from 'lucide-react';
import { WarmVmPanel } from './WarmVmPanel';
import type { Project } from '../types/project';

interface FleetDetailDrawerProps {
  onClose: () => void;
  onViewTask: (taskId: string) => void;
  projects: Project[];
}

export function FleetDetailDrawer({ onClose, onViewTask, projects }: FleetDetailDrawerProps) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-2xl h-full bg-neutral-950 border-l border-neutral-800 shadow-2xl flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-800 shrink-0">
          <h2 className="text-sm font-semibold text-neutral-200">VM Fleet</h2>
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
          <WarmVmPanel onViewTask={onViewTask} projects={projects} />
        </div>
      </div>
    </div>
  );
}
