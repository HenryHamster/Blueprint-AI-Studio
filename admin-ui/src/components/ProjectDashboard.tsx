import { useState } from 'react';
import { ChevronDown, ChevronUp, FolderKanban, GitBranch, Compass, UserPlus, Plus, Settings } from 'lucide-react';
import type { Task } from './TaskCard';
import { parseTimestamp } from '../utils/time';

export type { Project } from '../types/project';
import type { Project } from '../types/project';

interface ProjectDashboardProps {
  projects: Project[];
  tasks: Task[];
  selectedProjectId: string | null;
  onSelectProject: (projectId: string | null) => void;
  showMembers?: boolean;
  onToggleMembers?: () => void;
  userRole?: 'admin' | 'user' | null;
  onNewProject?: () => void;
  onOpenSetupGuide?: (projectId: string) => void;
}

/** A project is considered "new / setup incomplete" if it was created within the last hour
 *  and has no completed tasks yet. */
function isNewProject(project: Project, completedCount: number): boolean {
  if (!project.created_at) return false;
  const createdMs = parseTimestamp(project.created_at).getTime();
  const oneHourAgo = Date.now() - 60 * 60 * 1000;
  return createdMs > oneHourAgo && completedCount === 0;
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

export function ProjectDashboard({ projects, tasks, selectedProjectId, onSelectProject, showMembers, onToggleMembers, userRole, onNewProject, onOpenSetupGuide }: ProjectDashboardProps) {
  const [collapsed, setCollapsed] = useState(() => {
    const stored = localStorage.getItem('projectDashboardCollapsed');
    return stored === null ? true : stored === 'true';
  });

  if (projects.length === 0) return null;

  return (
    <div className="mb-4 shrink-0">
      <div className="flex items-center justify-between mb-2">
        {/* reason: compact collapse toggle in dashboard header; text-[11px] single-row height */}
        <button
          data-viewport-allow-small-target
          onClick={() => {
            const next = !collapsed;
            setCollapsed(next);
            localStorage.setItem('projectDashboardCollapsed', String(next));
          }}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
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
        {userRole === 'admin' && onNewProject && (
          <button
            onClick={onNewProject}
            className="flex items-center justify-center gap-1 text-[11px] text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50 rounded min-h-[44px] min-w-[44px] px-2 transition-colors"
            title="Add new project"
          >
            <Plus className="w-3 h-3" />
            New
          </button>
        )}
      </div>

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
        <>
          {/* Horizontal-scrollable project carousel: cards stay at min-width, carousel scrolls on narrow viewports */}
          <div
            className="flex gap-3 overflow-x-auto pb-2 custom-scrollbar"
            data-viewport-allow-overflow
            data-viewport-allow-truncation
          >
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
            const showSetupNudge = isNewProject(project, counts.completed);

            return (
              <div
                key={project.id}
                className={`relative shrink-0 rounded-xl border p-3 min-w-[220px] max-w-[280px] text-left transition-all ${
                  isSelected
                    ? 'bg-neutral-800/80 border-neutral-600 ring-1 ring-neutral-600'
                    : 'bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700'
                }`}
              >
                {counts.failed > 0 && !showSetupNudge && (
                  <span className="absolute top-2 right-2 text-[9px] font-bold bg-red-500/15 text-red-400 border border-red-500/20 rounded px-1 py-0.5">{counts.failed} failed</span>
                )}
                {showSetupNudge && (
                  <span className="absolute top-2 right-2 text-[9px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/20 rounded px-1 py-0.5">setup</span>
                )}
                <div className="flex items-center justify-between mb-1.5">
                  {/* reason: project name label button in dense card header; text-xs single-row height */}
                  <button
                    data-viewport-allow-small-target
                    onClick={() => onSelectProject(isSelected ? null : project.id)}
                    className="text-xs font-semibold text-neutral-200 truncate pr-2 hover:text-white transition-colors text-left"
                  >
                    {project.name}
                  </button>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {isSelected && onToggleMembers && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onToggleMembers(); }}
                        className={`min-h-[44px] min-w-[44px] flex items-center justify-center rounded transition-colors ${
                          showMembers
                            ? 'bg-cyan-500/15 text-cyan-400'
                            : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50'
                        }`}
                        title="Project Members"
                      >
                        <UserPlus className="w-3.5 h-3.5" />
                      </button>
                    )}
                    {showSetupNudge && onOpenSetupGuide && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onOpenSetupGuide(project.id); }}
                        className="min-h-[44px] min-w-[44px] flex items-center justify-center rounded text-amber-400 hover:text-amber-300 hover:bg-amber-500/10 transition-colors"
                        title="Complete project setup"
                      >
                        <Settings className="w-3.5 h-3.5" />
                      </button>
                    )}
                    <span className={`w-2 h-2 rounded-full ${project.autonomous_enabled ? 'bg-emerald-500' : 'bg-red-500'}`}
                      title={project.autonomous_enabled ? 'Autonomous mode enabled' : 'Autonomous mode disabled'} />
                  </div>
                </div>

                {/* reason: project card select button wrapping compact info rows; h constrained by content */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => onSelectProject(isSelected ? null : project.id)}
                  className="block w-full text-left"
                >
                  <div className="flex items-center gap-1.5 mb-2">
                    <GitBranch className="w-3 h-3 text-neutral-500 shrink-0" />
                    <span className="text-[10px] text-neutral-500 font-mono truncate">
                      {project.github_owner}/{project.github_repo}
                    </span>
                  </div>

                  {(project.directions_count ?? project.directions?.length ?? 0) > 0 && (
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-[10px] text-neutral-500 flex items-center gap-0.5">
                        <Compass className="w-3 h-3" />
                        {project.directions_count ?? project.directions?.length}
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
        </>
      )}
    </div>
  );
}
