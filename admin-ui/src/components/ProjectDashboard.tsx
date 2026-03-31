import { useState } from 'react';
import { ChevronDown, ChevronUp, FolderKanban, GitBranch, Compass } from 'lucide-react';
import type { Task } from './TaskCard';

export interface Project {
  id: string;
  name: string;
  status: string;
  github_owner: string;
  github_repo: string;
  autonomous_enabled: boolean;
  directions: unknown[];
  created_at: string;
}

interface ProjectDashboardProps {
  projects: Project[];
  tasks: Task[];
  selectedProjectId: string | null;
  onSelectProject: (projectId: string | null) => void;
}

const STATUS_COLORS: Record<string, string> = {
  active: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  paused: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  completed: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  archived: 'bg-neutral-500/10 text-neutral-400 border-neutral-500/20',
};

function getTaskCounts(tasks: Task[], projectId: string) {
  const projectTasks = tasks.filter(t => t.project_id === projectId);
  return {
    queued: projectTasks.filter(t => t.status === 'QUEUED').length,
    running: projectTasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING' || t.status === 'EVALUATING').length,
    failed: projectTasks.filter(t => t.status === 'FAILED').length,
    completed: projectTasks.filter(t => t.status === 'COMPLETED').length,
    total: projectTasks.length,
  };
}

export function ProjectDashboard({ projects, tasks, selectedProjectId, onSelectProject }: ProjectDashboardProps) {
  const [collapsed, setCollapsed] = useState(false);

  if (projects.length === 0) return null;

  return (
    <div className="mb-4 shrink-0">
      <button
        onClick={() => setCollapsed(c => !c)}
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
            const statusStyle = STATUS_COLORS[project.status] || STATUS_COLORS.active;

            return (
              <button
                key={project.id}
                onClick={() => onSelectProject(isSelected ? null : project.id)}
                className={`shrink-0 rounded-xl border p-3 min-w-[220px] max-w-[280px] text-left transition-all ${
                  isSelected
                    ? 'bg-neutral-800/80 border-neutral-600 ring-1 ring-neutral-600'
                    : 'bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700'
                }`}
              >
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-xs font-semibold text-neutral-200 truncate pr-2">{project.name}</span>
                  <span className={`shrink-0 w-2 h-2 rounded-full ${project.autonomous_enabled ? 'bg-emerald-500' : 'bg-red-500'}`}
                    title={project.autonomous_enabled ? 'Autonomous enabled' : 'Autonomous disabled'} />
                </div>

                <div className="flex items-center gap-1.5 mb-2">
                  <GitBranch className="w-3 h-3 text-neutral-500 shrink-0" />
                  <span className="text-[10px] text-neutral-500 font-mono truncate">
                    {project.github_owner}/{project.github_repo}
                  </span>
                </div>

                <div className="flex items-center gap-2 mb-2">
                  <span className={`text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded border ${statusStyle}`}>
                    {project.status}
                  </span>
                  {project.directions.length > 0 && (
                    <span className="text-[10px] text-neutral-500 flex items-center gap-0.5">
                      <Compass className="w-3 h-3" />
                      {project.directions.length}
                    </span>
                  )}
                </div>

                <div className="flex gap-2 text-[10px] font-mono">
                  {counts.queued > 0 && <span className="text-neutral-400">{counts.queued} queued</span>}
                  {counts.running > 0 && <span className="text-blue-400">{counts.running} running</span>}
                  {counts.failed > 0 && <span className="text-red-400">{counts.failed} failed</span>}
                  {counts.completed > 0 && <span className="text-emerald-400">{counts.completed} done</span>}
                  {counts.total === 0 && <span className="text-neutral-600">no tasks</span>}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
