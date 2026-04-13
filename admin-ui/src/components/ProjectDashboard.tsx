import { useState } from 'react';
import { ChevronDown, ChevronUp, FolderKanban, GitBranch, Compass, UserPlus } from 'lucide-react';
import type { Task } from './TaskCard';

export type { Project } from '../types/project';
import type { Project } from '../types/project';

interface ProjectDashboardProps {
  projects: Project[];
  tasks: Task[];
  selectedProjectId: string | null;
  onSelectProject: (projectId: string | null) => void;
  showMembers?: boolean;
  onToggleMembers?: () => void;
}


function getTaskCounts(tasks: Task[], projectId: string) {
  const projectTasks = tasks.filter(t => t.project_id === projectId);
  return {
    queued: projectTasks.filter(t => t.status === 'QUEUED').length,
    running: projectTasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length,
    evaluating: projectTasks.filter(t => t.status === 'EVALUATING').length,
    failed: projectTasks.filter(t => t.status === 'FAILED').length,
    completed: projectTasks.filter(t => t.status === 'COMPLETED').length,
    total: projectTasks.length,
  };
}

export function ProjectDashboard({ projects, tasks, selectedProjectId, onSelectProject, showMembers, onToggleMembers }: ProjectDashboardProps) {
  const [collapsed, setCollapsed] = useState(() => {
    const stored = localStorage.getItem('projectDashboardCollapsed');
    return stored === null ? true : stored === 'true';
  });

  if (projects.length === 0) return null;

  return (
    <div className="mb-4 shrink-0">
      <button
        onClick={() => {
          const next = !collapsed;
          setCollapsed(next);
          localStorage.setItem('projectDashboardCollapsed', String(next));
        }}
        className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2 hover:text-neutral-300 transition-colors"
      >
        <FolderKanban className="w-3.5 h-3.5" />
        Projects
        {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
        {selectedProjectId && (
          <span className="text-[10px] normal-case tracking-normal font-medium text-blue-400 ml-1">
            (filtered)
          </span>
        )}
      </button>

      {!collapsed && (
        <div className="flex gap-4 px-1 pb-2">
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
            {projects.filter(p => p.status === 'active').length} active
          </span>
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" />
            {projects.filter(p => p.status === 'paused').length} paused
          </span>
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0" />
            {projects.filter(p => p.autonomous_enabled).length} autonomous
          </span>
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0" />
            {projects.length} total
          </span>
        </div>
      )}

      {!collapsed && (
        <div className="flex gap-3 overflow-x-auto pb-2 custom-scrollbar">
          {/* All Projects option */}
          <button
            onClick={() => onSelectProject(null)}
            className={`shrink-0 rounded-xl border p-3 min-w-[180px] text-left transition-all ${
              selectedProjectId === null
                ? 'bg-neutral-800/80 border-neutral-600 ring-1 ring-neutral-600'
                : 'bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700'
            }`}
          >
            <div className="text-xs font-semibold text-neutral-200 mb-1">All Projects</div>
            <div className="text-[10px] text-neutral-500 font-mono">{tasks.length} tasks total</div>
          </button>

          {projects.map(project => {
            const counts = getTaskCounts(tasks, project.id);
            const isSelected = selectedProjectId === project.id;

            return (
              <div
                key={project.id}
                className={`relative shrink-0 rounded-xl border p-3 min-w-[220px] max-w-[280px] text-left transition-all ${
                  isSelected
                    ? 'bg-neutral-800/80 border-neutral-600 ring-1 ring-neutral-600'
                    : 'bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700'
                }`}
              >
                {counts.failed > 0 && (
                  <span className="absolute top-2 right-2 text-[9px] font-bold bg-red-500/15 text-red-400 border border-red-500/20 rounded px-1 py-0.5">{counts.failed} failed</span>
                )}
                <div className="flex items-center justify-between mb-1.5">
                  <button
                    onClick={() => onSelectProject(isSelected ? null : project.id)}
                    className="text-xs font-semibold text-neutral-200 truncate pr-2 hover:text-white transition-colors text-left"
                  >
                    {project.name}
                  </button>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {isSelected && onToggleMembers && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onToggleMembers(); }}
                        className={`p-1 rounded transition-colors ${
                          showMembers
                            ? 'bg-cyan-500/15 text-cyan-400'
                            : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50'
                        }`}
                        title="Project Members"
                      >
                        <UserPlus className="w-3.5 h-3.5" />
                      </button>
                    )}
                    <span className={`w-2 h-2 rounded-full ${project.autonomous_enabled ? 'bg-emerald-500' : 'bg-red-500'}`}
                      title={project.autonomous_enabled ? 'Autonomous mode enabled' : 'Autonomous mode disabled'} />
                  </div>
                </div>

                <button
                  onClick={() => onSelectProject(isSelected ? null : project.id)}
                  className="block w-full text-left"
                >
                  <div className="flex items-center gap-1.5 mb-2">
                    <GitBranch className="w-3 h-3 text-neutral-500 shrink-0" />
                    <span className="text-[10px] text-neutral-500 font-mono truncate">
                      {project.github_owner}/{project.github_repo}
                    </span>
                  </div>

                  {project.directions.length > 0 && (
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-[10px] text-neutral-500 flex items-center gap-0.5">
                        <Compass className="w-3 h-3" />
                        {project.directions.length}
                      </span>
                    </div>
                  )}

                  <div className="flex gap-2 text-[10px] font-mono">
                    {counts.queued > 0 && <span className="text-neutral-400">{counts.queued} queued</span>}
                    {counts.running > 0 && <span className="text-blue-400">{counts.running} running</span>}
                    {counts.evaluating > 0 && <span className="text-amber-400">{counts.evaluating} evaluating</span>}
                    {counts.failed > 0 && <span className="text-red-400">{counts.failed} failed</span>}
                    {counts.completed > 0 && <span className="text-emerald-400">{counts.completed} done</span>}
                    {counts.total === 0 && <span className="text-neutral-600">no tasks</span>}
                  </div>
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
