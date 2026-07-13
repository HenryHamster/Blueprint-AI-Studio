import { useState } from 'react';
import { X } from 'lucide-react';
import { WarmVmPanel } from './WarmVmPanel';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import type { Project } from '../types/project';

interface FleetDetailDrawerProps {
  onClose: () => void;
  onViewTask: (taskId: string) => void;
  projects: Project[];
  selectedProjectId?: string | null;
  fleetRunnersRefreshSignal?: number;
  live?: boolean;
}

type Tab = 'vm-fleet' | 'runners';

export function FleetDetailDrawer({ onClose, onViewTask, projects, selectedProjectId, fleetRunnersRefreshSignal, live }: FleetDetailDrawerProps) {
  const [tab, setTab] = useState<Tab>('vm-fleet');

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

        <div className="flex border-b border-neutral-800 shrink-0 px-4">
          <button
            onClick={() => setTab('vm-fleet')}
            className={`py-2 px-3 text-xs font-medium border-b-2 transition-colors ${tab === 'vm-fleet' ? 'border-blue-500 text-blue-400' : 'border-transparent text-neutral-400 hover:text-neutral-200'}`}
          >
            VM Fleet
          </button>
          <button
            onClick={() => setTab('runners')}
            className={`py-2 px-3 text-xs font-medium border-b-2 transition-colors ${tab === 'runners' ? 'border-blue-500 text-blue-400' : 'border-transparent text-neutral-400 hover:text-neutral-200'}`}
          >
            Self-hosted runners
          </button>
        </div>

        <div className="flex-1 overflow-auto custom-scrollbar">
          {tab === 'vm-fleet' && (
            <WarmVmPanel onViewTask={onViewTask} projects={projects} />
          )}
          {tab === 'runners' && (
            selectedProjectId ? (
              <FleetRunnersPanel projectId={selectedProjectId} refreshSignal={fleetRunnersRefreshSignal} live={live} onNavigateToTask={onViewTask} />
            ) : (
              <div className="p-6 text-xs text-neutral-500 text-center">
                Select a project to manage self-hosted runners.
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}
