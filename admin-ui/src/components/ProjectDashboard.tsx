import { useEffect, useState } from 'react';
import { ChevronDown, ChevronUp, FolderKanban, GitBranch, Compass, UserPlus, Plus, Settings, MoreVertical, Trash2, AlertTriangle, RotateCcw, Pause, Play, Archive, ArchiveRestore } from 'lucide-react';
import type { Task } from './TaskCard';
import { parseTimestamp } from '../utils/time';
import { projectStatusBadge } from '../utils/projectStatus';
import { ProjectApprovalGateSection } from './ProjectApprovalGateSection';
import { fetchApprovalGateConfig, updateApprovalGateConfig, deleteProject, resetProject, setProjectPaused, archiveProject, unarchiveProject, fetchArchivedProjects, type ApprovalGateConfigResponse } from '../api';

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
  /** Called after a project is successfully deleted, so the parent can refetch
   *  the project list and clear any selection pointing at the removed project. */
  onProjectDeleted?: (projectId: string) => void;
  /** Called after a project is successfully reset, so the parent can refetch
   *  the project + task lists (the project row survives; its tasks are wiped). */
  onProjectReset?: (projectId: string) => void;
  /** Called after a project's pause state is successfully toggled, so the parent
   *  can refetch the project list and reflect the new paused flag. */
  onProjectPaused?: (projectId: string) => void;
  /** Called after a project is successfully archived, so the parent can refetch
   *  the active list and clear any selection pointing at the archived project. */
  onProjectArchived?: (projectId: string) => void;
  /** Called after a project is successfully unarchived, so the parent can
   *  refetch the active list (the project reappears in it). */
  onProjectUnarchived?: (projectId: string) => void;
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
    blocked: projectTasks.filter(t => t.status === 'BLOCKED').length,
    failed: projectTasks.filter(t => t.status === 'FAILED').length,
    completed: projectTasks.filter(t => t.status === 'COMPLETED').length,
    total: projectTasks.length,
  };
}

function getMergeGateBadge(project: Project) {
  const verdict = project.merge_verification_verdict ?? { state: 'ci_checks' as const, test_commands: [] };
  const buildConfig = project.build_config ?? null;

  if (
    verdict.state === 'ci_checks' &&
    buildConfig?.has_ci === false &&
    buildConfig.requires_ci_checks !== false
  ) {
    return {
      label: 'Stall risk',
      classes: 'bg-red-500/15 text-red-300 border-red-500/20',
      title: 'No CI detected while required CI checks remain on. PRs will stall until the merge backstop times out.',
    };
  }

  if (verdict.state === 'tests_gated') {
    return {
      label: 'Tests gated',
      classes: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/20',
      title: "Blueprint will run the project's detected test command(s) before merging PRs.",
    };
  }

  if (verdict.state === 'unverified') {
    return {
      label: 'UNVERIFIED',
      classes: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/30',
      title: 'No test command was detected. PRs merge without verification.',
    };
  }

  return {
    label: 'CI gated',
    classes: 'bg-blue-500/15 text-blue-300 border-blue-500/20',
    title: "Blueprint will wait for the repository's GitHub CI checks before merging PRs.",
  };
}

export function ProjectDashboard({ projects, tasks, selectedProjectId, onSelectProject, showMembers, onToggleMembers, userRole, onNewProject, onOpenSetupGuide, onProjectDeleted, onProjectReset, onProjectPaused, onProjectArchived, onProjectUnarchived }: ProjectDashboardProps) {
  const [collapsed, setCollapsed] = useState(() => {
    const stored = localStorage.getItem('projectDashboardCollapsed');
    return stored === null ? true : stored === 'true';
  });
  const [approvalGateState, setApprovalGateState] = useState<ApprovalGateConfigResponse | null>(null);
  // Per-project delete affordance state. Only one project's menu/confirmation
  // is open at a time, keyed by project id.
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Per-project reset-to-clean affordance state, mirroring the delete flow.
  const [confirmingResetId, setConfirmingResetId] = useState<string | null>(null);
  const [resettingId, setResettingId] = useState<string | null>(null);
  const [resetError, setResetError] = useState<string | null>(null);
  // Per-project pause/resume affordance state. Pause is reversible, so it is a
  // direct menu action (no destructive confirmation panel); only in-flight and
  // error state are tracked, keyed by project id.
  const [pausingId, setPausingId] = useState<string | null>(null);
  const [pauseError, setPauseError] = useState<string | null>(null);
  // Per-project archive affordance state, mirroring the delete/reset flow.
  const [confirmingArchiveId, setConfirmingArchiveId] = useState<string | null>(null);
  const [archivingId, setArchivingId] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  // Archived-projects sub-view (lazy-loaded, owned locally so the parent's
  // active-project props stay authoritative).
  const [showArchived, setShowArchived] = useState(false);
  const [archivedProjects, setArchivedProjects] = useState<Project[]>([]);
  const [archivedLoading, setArchivedLoading] = useState(false);
  const [archivedError, setArchivedError] = useState<string | null>(null);
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null);

  const closeProjectActions = () => {
    setMenuOpenId(null);
    setConfirmingId(null);
    setDeleteError(null);
    setConfirmingResetId(null);
    setResetError(null);
    setPauseError(null);
    setConfirmingArchiveId(null);
    setArchiveError(null);
  };

  const loadArchivedProjects = async () => {
    setArchivedLoading(true);
    setArchivedError(null);
    try {
      const items = await fetchArchivedProjects();
      setArchivedProjects(items);
    } catch (e) {
      setArchivedError(e instanceof Error ? e.message : 'Failed to load archived projects');
    } finally {
      setArchivedLoading(false);
    }
  };

  const handleToggleArchived = () => {
    const next = !showArchived;
    setShowArchived(next);
    if (next) void loadArchivedProjects();
  };

  const handleArchiveProject = async (projectId: string) => {
    setArchivingId(projectId);
    setArchiveError(null);
    try {
      await archiveProject(projectId);
      closeProjectActions();
      onProjectArchived?.(projectId);
      // Keep the archived sub-view in sync if it's currently open.
      if (showArchived) void loadArchivedProjects();
    } catch (e) {
      setArchiveError(e instanceof Error ? e.message : 'Failed to archive project');
    } finally {
      setArchivingId(null);
    }
  };

  const handleUnarchiveProject = async (projectId: string) => {
    setUnarchivingId(projectId);
    setArchivedError(null);
    try {
      await unarchiveProject(projectId);
      onProjectUnarchived?.(projectId);
      // Drop it from the local archived list immediately, then refetch to stay
      // authoritative against the server.
      setArchivedProjects(prev => prev.filter(p => p.id !== projectId));
      void loadArchivedProjects();
    } catch (e) {
      setArchivedError(e instanceof Error ? e.message : 'Failed to unarchive project');
    } finally {
      setUnarchivingId(null);
    }
  };

  const handleDeleteProject = async (projectId: string) => {
    setDeletingId(projectId);
    setDeleteError(null);
    try {
      // Always force: the confirmation explicitly warns that active work will
      // be cancelled, so we never want the 409 "cancel them first" refusal.
      await deleteProject(projectId, true);
      closeProjectActions();
      onProjectDeleted?.(projectId);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Failed to delete project');
    } finally {
      setDeletingId(null);
    }
  };

  const handleResetProject = async (projectId: string) => {
    setResettingId(projectId);
    setResetError(null);
    try {
      await resetProject(projectId);
      closeProjectActions();
      onProjectReset?.(projectId);
    } catch (e) {
      setResetError(e instanceof Error ? e.message : 'Failed to reset project');
    } finally {
      setResettingId(null);
    }
  };
  const handleTogglePaused = async (projectId: string, nextPaused: boolean) => {
    setPausingId(projectId);
    setPauseError(null);
    try {
      await setProjectPaused(projectId, nextPaused);
      closeProjectActions();
      onProjectPaused?.(projectId);
    } catch (e) {
      setPauseError(e instanceof Error ? e.message : 'Failed to update pause state');
    } finally {
      setPausingId(null);
    }
  };

  const selectedProject = projects.find(p => p.id === selectedProjectId) ?? null;
  const approvalGateData = selectedProjectId
    ? (approvalGateState?.project_id === selectedProjectId
      ? approvalGateState
      : { project_id: selectedProjectId, approval_gate_config: { infra_files: [], critical_files: [] }, is_default: true })
    : null;

  useEffect(() => {
    if (!selectedProjectId) return;
    let cancelled = false;
    fetchApprovalGateConfig(selectedProjectId)
      .then(data => {
        if (cancelled) return;
        setApprovalGateState(data?.approval_gate_config ? data : {
          project_id: selectedProjectId,
          approval_gate_config: { infra_files: [], critical_files: [] },
          is_default: true,
        });
      })
      .catch(() => {
        if (!cancelled) {
          setApprovalGateState({
            project_id: selectedProjectId,
            approval_gate_config: { infra_files: [], critical_files: [] },
            is_default: true,
          });
        }
      });
    return () => { cancelled = true; };
  }, [selectedProjectId]);

  const handleSaveApprovalGate = async (config: ApprovalGateConfigResponse['approval_gate_config']) => {
    if (!selectedProjectId) return;
    const data = await updateApprovalGateConfig(selectedProjectId, config);
    setApprovalGateState(data?.approval_gate_config ? data : {
      project_id: selectedProjectId,
      approval_gate_config: config,
      is_default: false,
    });
  };

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
        <div className="flex items-center gap-1">
          <button
            onClick={handleToggleArchived}
            className={`flex items-center justify-center gap-1 text-[11px] rounded min-h-[44px] px-2 transition-colors ${
              showArchived
                ? 'bg-sky-500/15 text-sky-300'
                : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
            }`}
            title="Show archived projects"
            aria-pressed={showArchived}
          >
            <Archive className="w-3 h-3" />
            {showArchived ? 'Hide archived' : 'Show archived'}
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
      </div>

      {!collapsed && (
        <div className="flex gap-4 px-1 pb-2">
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
            {projects.filter(p => p.status === 'active').length} active
          </span>
          <span className="bg-neutral-800 rounded px-2 py-0.5 text-xs text-neutral-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" />
            {projects.filter(p => p.paused).length} paused
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
                    {(() => {
                      const badge = projectStatusBadge(project.status);
                      return (
                        <span
                          className={`text-[9px] font-bold rounded px-1 py-0.5 ${badge.classes}`}
                          title={`Project status: ${badge.label}`}
                        >
                          {badge.label}
                        </span>
                      );
                    })()}
                    {(() => {
                      const badge = getMergeGateBadge(project);
                      return (
                        <span
                          className={`flex items-center gap-0.5 text-[9px] font-bold rounded px-1 py-0.5 border ${badge.classes}`}
                          title={badge.title}
                        >
                          {badge.label}
                        </span>
                      );
                    })()}
                    {project.paused && (
                      <span
                        className="flex items-center gap-0.5 text-[9px] font-bold rounded px-1 py-0.5 bg-orange-500/15 text-orange-400 border border-orange-500/20"
                        title="Project is paused: no new work will be dispatched"
                      >
                        <Pause className="w-2.5 h-2.5" />
                        Paused
                      </span>
                    )}
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
                    {project.my_role === 'owner' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setMenuOpenId(menuOpenId === project.id ? null : project.id);
                          setConfirmingId(null);
                          setDeleteError(null);
                          setPauseError(null);
                        }}
                        className="min-h-[44px] min-w-[44px] flex items-center justify-center rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                        title="Project actions"
                        aria-haspopup="menu"
                        aria-expanded={menuOpenId === project.id}
                      >
                        <MoreVertical className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                </div>

                {menuOpenId === project.id && confirmingId !== project.id && confirmingResetId !== project.id && confirmingArchiveId !== project.id && (
                  <div className="mb-2 rounded-lg border border-neutral-800 bg-neutral-900/80 p-1">
                    <button
                      onClick={(e) => { e.stopPropagation(); void handleTogglePaused(project.id, !project.paused); }}
                      disabled={pausingId === project.id}
                      className="w-full flex items-center gap-2 min-h-[44px] px-2 rounded text-[11px] text-orange-400 hover:bg-orange-500/10 transition-colors text-left disabled:opacity-50"
                      title={project.paused
                        ? 'Resume project: re-enable dispatch of queued work'
                        : 'Pause project: stop dispatching new work (in-flight tasks drain)'}
                    >
                      {project.paused
                        ? <Play className="w-3.5 h-3.5 shrink-0" />
                        : <Pause className="w-3.5 h-3.5 shrink-0" />}
                      {pausingId === project.id
                        ? (project.paused ? 'Resuming…' : 'Pausing…')
                        : (project.paused ? 'Resume project' : 'Pause project')}
                    </button>
                    {pauseError && (
                      <p className="text-[10px] text-orange-300 bg-orange-500/10 border border-orange-500/20 rounded px-1.5 py-1 mb-1 mx-1 break-words">
                        {pauseError}
                      </p>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); setConfirmingArchiveId(project.id); setArchiveError(null); }}
                      className="w-full flex items-center gap-2 min-h-[44px] px-2 rounded text-[11px] text-sky-400 hover:bg-sky-500/10 transition-colors text-left"
                      title="Archive project (reversible — hides it and halts new work)"
                    >
                      <Archive className="w-3.5 h-3.5 shrink-0" />
                      Archive project
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); setConfirmingResetId(project.id); setResetError(null); }}
                      className="w-full flex items-center gap-2 min-h-[44px] px-2 rounded text-[11px] text-amber-400 hover:bg-amber-500/10 transition-colors text-left"
                      title="Reset project to a clean, runnable state (keeps config)"
                    >
                      <RotateCcw className="w-3.5 h-3.5 shrink-0" />
                      Reset to clean
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); setConfirmingId(project.id); setDeleteError(null); }}
                      className="w-full flex items-center gap-2 min-h-[44px] px-2 rounded text-[11px] text-red-400 hover:bg-red-500/10 transition-colors text-left"
                      title="Delete project"
                    >
                      <Trash2 className="w-3.5 h-3.5 shrink-0" />
                      Delete project
                    </button>
                  </div>
                )}

                {confirmingArchiveId === project.id && (
                  <div className="mb-2 rounded-lg border border-sky-500/30 bg-sky-950/30 p-2" data-viewport-allow-truncation>
                    <div className="flex items-start gap-1.5 mb-2">
                      <Archive className="w-3.5 h-3.5 text-sky-400 shrink-0 mt-0.5" />
                      <p className="text-[11px] text-sky-100 leading-snug">
                        Archive <span className="font-semibold">{project.name}</span>?
                        It will be hidden from the active dashboard and stop generating new work.
                        <span className="block mt-0.5 text-sky-200/90">
                          Reversible — restore it any time from <span className="font-semibold">Show archived</span>.
                        </span>
                      </p>
                    </div>
                    {archiveError && (
                      <p className="text-[10px] text-sky-200 bg-sky-500/10 border border-sky-500/20 rounded px-1.5 py-1 mb-2 break-words">
                        {archiveError}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleArchiveProject(project.id); }}
                        disabled={archivingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-sky-500/20 text-sky-200 hover:bg-sky-500/30 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Confirm archive"
                      >
                        <Archive className="w-3.5 h-3.5" />
                        {archivingId === project.id ? 'Archiving…' : 'Archive'}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); closeProjectActions(); }}
                        disabled={archivingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center rounded bg-neutral-800 text-neutral-300 hover:bg-neutral-700 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Cancel archive"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {confirmingResetId === project.id && (
                  <div className="mb-2 rounded-lg border border-amber-500/30 bg-amber-950/30 p-2" data-viewport-allow-truncation>
                    <div className="flex items-start gap-1.5 mb-2">
                      <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                      <p className="text-[11px] text-amber-100 leading-snug">
                        Reset <span className="font-semibold">{project.name}</span> to clean?
                        <span className="block mt-1 text-amber-200/90">
                          <span className="font-semibold">Wiped:</span> all task history and runtime state
                          (active tasks force-cancelled, their VMs torn down).
                        </span>
                        <span className="block mt-0.5 text-amber-200/90">
                          <span className="font-semibold">Kept:</span> the project and all its configuration
                          (goal, directions, branch settings, build config, problem model, members).
                        </span>
                      </p>
                    </div>
                    {resetError && (
                      <p className="text-[10px] text-amber-200 bg-amber-500/10 border border-amber-500/20 rounded px-1.5 py-1 mb-2 break-words">
                        {resetError}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleResetProject(project.id); }}
                        disabled={resettingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-amber-500/20 text-amber-200 hover:bg-amber-500/30 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Confirm reset"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        {resettingId === project.id ? 'Resetting…' : 'Reset to clean'}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); closeProjectActions(); }}
                        disabled={resettingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center rounded bg-neutral-800 text-neutral-300 hover:bg-neutral-700 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Cancel reset"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {confirmingId === project.id && (
                  <div className="mb-2 rounded-lg border border-red-500/30 bg-red-950/30 p-2" data-viewport-allow-truncation>
                    <div className="flex items-start gap-1.5 mb-2">
                      <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0 mt-0.5" />
                      <p className="text-[11px] text-red-200 leading-snug">
                        Delete <span className="font-semibold">{project.name}</span>? Any active tasks will be
                        force-cancelled, their VMs torn down, and the project permanently removed.
                      </p>
                    </div>
                    {deleteError && (
                      <p className="text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 mb-2 break-words">
                        {deleteError}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleDeleteProject(project.id); }}
                        disabled={deletingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-red-500/20 text-red-300 hover:bg-red-500/30 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Confirm delete"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                        {deletingId === project.id ? 'Deleting…' : 'Delete'}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); closeProjectActions(); }}
                        disabled={deletingId === project.id}
                        className="flex-1 min-h-[44px] flex items-center justify-center rounded bg-neutral-800 text-neutral-300 hover:bg-neutral-700 text-[11px] font-semibold transition-colors disabled:opacity-50"
                        title="Cancel delete"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

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
                    {counts.blocked > 0 && <span className="text-amber-500">{counts.blocked} blocked</span>}
                    {counts.failed > 0 && <span className="text-red-400">{counts.failed} failed</span>}
                    {counts.completed > 0 && <span className="text-emerald-400">{counts.completed} done</span>}
                    {counts.total === 0 && <span className="text-neutral-600">no tasks</span>}
                  </div>
                </button>
              </div>
            );
          })}
          </div>
          {selectedProject && (
            <div className="mt-4 rounded-xl border border-neutral-800 bg-neutral-950/60 p-4">
              <ProjectApprovalGateSection
                data={approvalGateData}
                canSave={selectedProject.my_role === 'owner'}
                onSave={handleSaveApprovalGate}
              />
            </div>
          )}
        </>
      )}

      {showArchived && (
        <div className="mt-2 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3" data-viewport-allow-truncation>
          <div className="flex items-center gap-2 mb-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest">
            <Archive className="w-3.5 h-3.5" />
            Archived projects
          </div>
          {archivedLoading && (
            <p className="text-[11px] text-neutral-500">Loading archived projects…</p>
          )}
          {archivedError && (
            <p className="text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 mb-2 break-words">
              {archivedError}
            </p>
          )}
          {!archivedLoading && !archivedError && archivedProjects.length === 0 && (
            <p className="text-[11px] text-neutral-600">No archived projects.</p>
          )}
          {archivedProjects.length > 0 && (
            <div className="flex flex-col gap-1.5">
              {archivedProjects.map(project => (
                <div
                  key={project.id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-neutral-800/60 bg-neutral-900/50 px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-neutral-300 truncate">{project.name}</div>
                    <div className="flex items-center gap-1.5 text-[10px] text-neutral-500 font-mono truncate">
                      <GitBranch className="w-3 h-3 shrink-0" />
                      {project.github_owner}/{project.github_repo}
                    </div>
                  </div>
                  {project.my_role === 'owner' && (
                    <button
                      onClick={() => void handleUnarchiveProject(project.id)}
                      disabled={unarchivingId === project.id}
                      className="shrink-0 min-h-[44px] flex items-center justify-center gap-1.5 rounded bg-sky-500/20 text-sky-200 hover:bg-sky-500/30 text-[11px] font-semibold px-3 transition-colors disabled:opacity-50"
                      title="Restore project to the active list"
                    >
                      <ArchiveRestore className="w-3.5 h-3.5" />
                      {unarchivingId === project.id ? 'Restoring…' : 'Restore'}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
